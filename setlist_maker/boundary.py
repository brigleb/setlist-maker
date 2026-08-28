"""Adaptive boundary detection engine.

Pure: no I/O, no clock, no network. The engine consumes completed `Probe`s and
answers "what should be probed next?" (`next_probe`) and "what does the
evidence say the recording contains?" (`segments`). All state is a
deterministic fold over the probe sequence -- replaying the same probes in the
same order rebuilds the identical engine, which is what makes resume "load the
probe list and replay it" (see the design spec).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from statistics import median

from setlist_maker.identify import _assign_cluster, _normalized_key

# Virtual identities for the recording's edges. Real identities are
# (artist, title) cluster keys or None (an unidentified probe), so these
# strings can never collide with them.
START = "<start>"
END = "<end>"


@dataclass(frozen=True)
class Probe:
    """One completed Shazam sample: where it looked and what came back."""

    t: float
    window: float
    purpose: str  # "coverage" | "refine"
    result: dict | None
    offsets: list[dict] | None = None

    @property
    def mid(self) -> float:
        return self.t + self.window / 2.0


@dataclass(frozen=True)
class ProbePlan:
    """What the scheduler wants probed next."""

    t: float
    window: float
    purpose: str


@dataclass
class EngineConfig:
    """Tunable knobs; the interesting ones surface as CLI flags (see spec)."""

    stride: float = 90.0  # max unprobed span between same-track evidence
    precision: float = 5.0  # boundary target width
    precision_none: float = 30.0  # target width when one side is unidentified
    coverage_window: float = 30.0
    refine_window: float = 12.0
    # shazamio_core fingerprints a *centered* excerpt of this length out of
    # whatever window it is handed (its SearchParams default), so a probe's
    # reported offset describes audio starting (window - this)/2 after the
    # window does. See `_probe_start_estimate`.
    fingerprint_segment: float = 10.0
    offset_tolerance: float = 4.0  # max spread among a track's T-O estimates
    timeskew_max: float = 0.02  # beyond this the playback was tempo-shifted
    min_corroboration: int = 2  # probes needed before offsets are trusted
    # How far after P the verification probe's *fingerprinted audio* starts
    # (not its window -- see `_fingerprint_lead`). The excerpt is
    # `fingerprint_segment` long and Shazam names whichever track dominates it,
    # so a cut-in is only missed when it is under `verify_lead + segment/2`;
    # at 0.0 that bound is exactly `precision`, which is what keeps a mistaken
    # prediction inside the boundary target instead of 3s past it.
    verify_lead: float = 0.0
    max_refines_per_gap: int = 12  # thrash cap between two coverage probes
    phantom_min: float = 20.0  # min resolved extent for a 1-probe track
    singleton_confidence_keep: float = 0.6
    title_threshold: float = 0.85
    artist_threshold: float = 0.9


@dataclass(frozen=True)
class Evidence:
    """A point on the timeline with a known identity (a probe's window midpoint,
    or a virtual endpoint)."""

    mid: float
    identity: object  # cluster key tuple, None, START or END
    probe: Probe | None  # None for the virtual endpoints


@dataclass(frozen=True)
class Segment:
    """One entry of the folded tracklist. `confidence` describes the *start*
    boundary: "resolved" (within target) or "coarse" (best effort so far)."""

    start: float
    info: dict | None
    confidence: str


class BoundaryEngine:
    def __init__(self, duration: float, config: EngineConfig | None = None):
        self.duration = float(duration)
        self.cfg = config or EngineConfig()
        self.probes: list[Probe] = []
        self._evidence: list[Evidence] = []  # sorted by mid; real probes only
        self._identity_by_index: list[object] = []  # parallel to self.probes
        self._clusters: list[tuple[str, str]] = []
        self._cluster_meta: dict[tuple[str, str], dict] = {}

    # ---- identity --------------------------------------------------------
    def _identify(self, result: dict | None) -> tuple[str, str] | None:
        """Assign a probe result to a fuzzy identity cluster (or None)."""
        if not result:
            return None
        key = _assign_cluster(
            _normalized_key(result),
            self._clusters,
            self.cfg.title_threshold,
            self.cfg.artist_threshold,
        )
        confidence = result.get("confidence") or 0
        best = self._cluster_meta.get(key)
        if best is None or confidence > (best.get("confidence") or 0):
            self._cluster_meta[key] = result
        return key

    # ---- fold ------------------------------------------------------------
    def add_probe(self, probe: Probe) -> list[dict]:
        """Fold one completed probe into the model and report what changed.

        Events are computed by snapshotting interval statuses and per-cluster
        predictions before/after the insert -- no incremental bookkeeping to
        drift out of sync with the fold."""
        before = {
            (round(left.mid, 3), round(right.mid, 3)): self._status(left, right)
            for left, right in self._pairs()
        }
        pred_before = {k: self._trusted_start(k) for k in self._clusters}
        enclosing = self._enclosing(probe.mid)

        # Peek at cluster novelty without mutating (real assignment below).
        is_new = False
        if probe.result:
            peek = _assign_cluster(
                _normalized_key(probe.result),
                list(self._clusters),
                self.cfg.title_threshold,
                self.cfg.artist_threshold,
            )
            is_new = peek not in self._clusters

        identity = self._identify(probe.result)
        ev = Evidence(mid=probe.mid, identity=identity, probe=probe)
        mids = [e.mid for e in self._evidence]
        self._evidence.insert(bisect.bisect_left(mids, ev.mid), ev)
        self.probes.append(probe)
        self._identity_by_index.append(identity)

        meta = (self._cluster_meta.get(identity) or {}) if identity else {}
        events: list[dict] = [
            {
                "type": "probe_result",
                "t": round(probe.t, 1),
                "window": probe.window,
                "purpose": probe.purpose,
                "artist": meta.get("artist"),
                "title": meta.get("title"),
                "confidence": (probe.result or {}).get("confidence"),
            }
        ]
        if is_new and identity is not None:
            events.append(
                {
                    "type": "track_discovered",
                    "artist": meta.get("artist"),
                    "title": meta.get("title"),
                    "at": round(probe.mid, 1),
                }
            )
        if (
            enclosing is not None
            and self._is_boundary(*enclosing)
            and identity == enclosing[0].identity
        ):
            p_start = self._trusted_start(enclosing[1].identity)
            if p_start is not None and self._excerpt_start(probe) >= p_start - 0.5:
                events.append(
                    {
                        "type": "cut_in_detected",
                        "at": round(probe.mid, 1),
                        "predicted": round(p_start, 1),
                    }
                )

        after = {
            (round(left.mid, 3), round(right.mid, 3)): self._status(left, right)
            for left, right in self._pairs()
        }
        if after.keys() - before.keys():
            events.append({"type": "interval_split", "at": round(probe.mid, 1)})
        for key, status in after.items():
            prev = before.get(key)
            if status == "retired" and prev != "retired":
                events.append(
                    {
                        "type": "interval_retired",
                        "left": round(key[0], 1),
                        "right": round(key[1], 1),
                    }
                )
            elif status == "resolved" and prev != "resolved":
                lo, hi = key
                pair = next(
                    (pl, pr)
                    for pl, pr in self._pairs()
                    if round(pl.mid, 3) == lo and round(pr.mid, 3) == hi
                )
                p_start = self._resolved_by_prediction(*pair)
                rmeta = self._cluster_meta.get(pair[1].identity) or {}
                events.append(
                    {
                        "type": "boundary_confirmed",
                        "start": round(p_start, 1),
                        "artist": rmeta.get("artist"),
                        "title": rmeta.get("title"),
                    }
                )
        for key in self._clusters:
            now = self._trusted_start(key)
            if now is not None and pred_before.get(key) is None:
                kmeta = self._cluster_meta.get(key) or {}
                events.append(
                    {
                        "type": "boundary_predicted",
                        "predicted_start": round(now, 1),
                        "artist": kmeta.get("artist"),
                        "title": kmeta.get("title"),
                    }
                )
        return events

    # ---- interval model --------------------------------------------------
    def _points(self) -> list[Evidence]:
        return [Evidence(0.0, START, None)] + self._evidence + [Evidence(self.duration, END, None)]

    def _pairs(self) -> list[tuple[Evidence, Evidence]]:
        pts = self._points()
        return list(zip(pts, pts[1:]))

    def _target(self, left: Evidence, right: Evidence) -> float:
        li, ri = left.identity, right.identity
        if li is START or ri is END:
            # An edge interval splits like a same-track one: the virtual
            # endpoint asserts nothing, so the only job here is coverage. This
            # is tested before the None branch deliberately -- a recording that
            # opens or closes on unidentifiable audio should still be *covered*
            # at the stride, not bisected to precision_none against a sentinel
            # that was never evidence of anything.
            return self.cfg.stride
        if li is None or ri is None:
            # An unidentified stretch: refine to roughly today's sequential
            # granularity, no finer -- Shazam already failed here once.
            return self.cfg.precision_none
        if li == ri:
            return self.cfg.stride
        return self.cfg.precision

    def _is_boundary(self, left: Evidence, right: Evidence) -> bool:
        li, ri = left.identity, right.identity
        return li not in (None, START, END) and ri not in (None, START, END) and li != ri

    def _enclosing(self, mid: float) -> tuple[Evidence, Evidence] | None:
        for left, right in self._pairs():
            if left.mid <= mid < right.mid:
                return (left, right)
        return None

    # ---- offset prediction ----------------------------------------------
    def _fingerprint_lead(self, window: float) -> float:
        """How far into a probe window the audio Shazam actually hears begins.

        Below the segment length the whole window is fingerprinted, so none."""
        return max(0.0, (window - self.cfg.fingerprint_segment) / 2.0)

    def _excerpt_start(self, probe: Probe) -> float:
        """Where the fingerprinted audio starts, in recording time."""
        return probe.t + self._fingerprint_lead(probe.window)

    def _probe_start_estimate(self, probe: Probe) -> float | None:
        """This probe's implied track start. A *lower bound*: a track the DJ cut
        into mid-song implies a start earlier than the real boundary, which is
        why prediction is verified after P, never before (see spec:
        Verification protocol).

        Not plain `T - O`. `Shazam.recognize` runs through shazamio_core, which
        fingerprints a **centered** `fingerprint_segment` (10s) excerpt of the
        window it is handed -- so the matched audio begins `lead` seconds after
        the probe does, and the offset describes *that* point. Measured on a
        real set (spec Errata): a 30s coverage probe and a 12s refine probe of
        the same track disagree by exactly 9.0s = (30-10)/2 - (12-10)/2 raw,
        and agree to within 0.1s once `lead` is subtracted out. Skipping this
        would not merely shift boundaries: the 9s disagreement exceeds
        `offset_tolerance`, so `_trusted_start` would reject every track probed
        at both window sizes and the prediction path would silently never fire.
        """
        if not probe.offsets:
            return None
        lead = self._fingerprint_lead(probe.window)
        cands = [
            probe.t + lead - m["offset"]
            for m in probe.offsets
            if isinstance(m.get("offset"), (int, float))
            and abs(m.get("timeskew") or 0.0) <= self.cfg.timeskew_max
        ]
        return median(cands) if cands else None

    def _trusted_start(self, key: object) -> float | None:
        """The cluster's predicted start, if enough probes agree on it."""
        starts = [
            est
            for p, ident in zip(self.probes, self._identity_by_index)
            if ident == key and (est := self._probe_start_estimate(p)) is not None
        ]
        if len(starts) < self.cfg.min_corroboration:
            return None
        if max(starts) - min(starts) > self.cfg.offset_tolerance:
            return None
        return median(starts)

    def _resolved_by_prediction(self, left: Evidence, right: Evidence) -> float | None:
        """The accepted boundary P for an A..B interval, or None.

        Pure predicate over the probe set: trusted P inside the interval, and
        some B probe whose *fingerprinted audio* began within
        [P - 0.5, P + precision] -- i.e. B was confirmed playing just after its
        predicted start. Measured from the excerpt rather than the window
        because those differ by 10s for a coverage probe, which would otherwise
        let one vouch for a boundary it never listened to. The verification
        probe the scheduler places at P + verify_lead satisfies this when it
        answers B; a cut-in (probe answers A) never can, because that probe's
        evidence becomes the interval's new left edge, pushing P outside."""
        if not self._is_boundary(left, right):
            return None
        key = right.identity
        p_start = self._trusted_start(key)
        if p_start is None or not (left.mid < p_start < right.mid):
            return None
        for p, ident in zip(self.probes, self._identity_by_index):
            if (
                ident == key
                and p_start - 0.5 <= self._excerpt_start(p) <= p_start + self.cfg.precision
            ):
                return p_start
        return None

    # ---- scheduling ------------------------------------------------------
    def next_probe(self) -> ProbePlan | None:
        """The highest-value probe to run next, or None when converged.

        Priority is width/target, so early pops are breadth-first coverage of
        the whole file and later pops tighten the worst boundary -- which is
        the anytime property: stopping after any prefix leaves the maximum
        remaining uncertainty as small as that many probes allowed."""
        if not self._evidence:
            if self.duration <= 1.0:
                return None
            window = min(self.cfg.coverage_window, self.duration)
            t = max(0.0, self.duration / 2.0 - window / 2.0)
            return ProbePlan(t=t, window=window, purpose="coverage")

        best: tuple[float, ProbePlan] | None = None
        for left, right in self._pairs():
            ratio = (right.mid - left.mid) / self._target(left, right)
            if ratio <= 1.0:
                continue
            if self._is_boundary(left, right) and not self._needs_coverage(left, right):
                if self._resolved_by_prediction(left, right) is not None:
                    continue
                if self._capped(left, right):
                    continue
            plan = self._plan_for(left, right)
            if plan is None:
                continue
            if best is None or ratio > best[0] + 1e-9:
                best = (ratio, plan)
        return best[1] if best else None

    def _plan_for(self, left: Evidence, right: Evidence) -> ProbePlan | None:
        cfg = self.cfg
        width = right.mid - left.mid
        if self._needs_coverage(left, right):
            # Too wide to characterise, whatever its endpoints claim.
            window, purpose = cfg.coverage_window, "coverage"
            mid = self._grid_mid(left, right)
        elif self._is_boundary(left, right):
            p_start = self._trusted_start(right.identity)
            if p_start is not None and left.mid < p_start < right.mid:
                # Offset the window so the *fingerprinted* excerpt lands where
                # the verification wants it, then clamp: an early P could
                # otherwise plan a negative start, and extract_window would
                # quietly hand back different audio than the probe records.
                t = p_start + cfg.verify_lead - self._fingerprint_lead(cfg.refine_window)
                t = min(max(t, 0.0), max(0.0, self.duration - cfg.refine_window))
                if t + cfg.refine_window / 2.0 < right.mid - 0.25 and not self._near_existing(
                    t, cfg.refine_window
                ):
                    return ProbePlan(t=t, window=cfg.refine_window, purpose="refine")
            window, purpose = cfg.refine_window, "refine"
            mid = (left.mid + right.mid) / 2.0
        else:  # None-adjacent: hunting identity, not a boundary
            window = cfg.coverage_window if width > cfg.coverage_window * 1.5 else cfg.refine_window
            purpose = "coverage"
            mid = (left.mid + right.mid) / 2.0

        t = mid - window / 2.0
        t = min(max(t, 0.0), max(0.0, self.duration - window))
        if not (left.mid + 0.25 < t + window / 2.0 < right.mid - 0.25):
            return None  # unprobeable sliver (edge clamping pushed us out)
        if self._near_existing(t, window):
            return None
        return ProbePlan(t=t, window=window, purpose=purpose)

    def _grid_mid(self, left: Evidence, right: Evidence) -> float:
        """Nearest stride multiple strictly inside the interval.

        Splitting on the stride grid instead of the raw midpoint means full
        coverage tiles the file in exactly duration/stride probes; blind
        halving can cost up to 2x that (14400/2^k first dips under 90 at
        56.25s spacing)."""
        s = self.cfg.stride
        mid = (left.mid + right.mid) / 2.0
        g = round(mid / s) * s
        if g <= left.mid + 0.5:
            g += s
        if g >= right.mid - 0.5:
            g -= s
        if not (left.mid + 0.5 < g < right.mid - 0.5):
            return mid
        return g

    def _near_existing(self, t: float, window: float) -> bool:
        """Would this probe fingerprint audio some earlier probe already heard?

        Compares the *excerpt* Shazam actually listens to, not the window
        start. Those differ by 9s between a 30s coverage probe and a 12s refine
        probe, so a window-start test gets it wrong in both directions: it
        blocks a refine probe that would hear entirely new audio -- measured,
        this stalled bisection at an 18s interval and left a 7.4s boundary
        error -- while permitting two probes that would hear the same ten
        seconds."""
        start = t + self._fingerprint_lead(window)
        return any(abs(self._excerpt_start(p) - start) < 0.5 for p in self.probes)

    def _coverage_gap(self, left: Evidence, right: Evidence) -> tuple[float, float]:
        """The span between the nearest coverage/virtual evidence either side."""
        lo, hi = 0.0, self.duration
        for e in self._points():
            anchored = e.probe is None or e.probe.purpose == "coverage"
            if anchored and e.mid <= left.mid:
                lo = e.mid
            if anchored and e.mid >= right.mid:
                hi = e.mid
                break
        return lo, hi

    def _capped(self, left: Evidence, right: Evidence) -> bool:
        """Thrash guard: a transition zone that keeps contradicting itself
        stops absorbing probes once its coverage gap has eaten the cap."""
        lo, hi = self._coverage_gap(left, right)
        n = sum(1 for p in self.probes if p.purpose == "refine" and lo <= p.mid <= hi)
        return n >= self.cfg.max_refines_per_gap

    def _needs_coverage(self, left: Evidence, right: Evidence) -> bool:
        """Wider than the stride, so a whole track could still be hiding here.

        Checked ahead of every boundary consideration: a predicted boundary
        retires its interval only once the interval is too narrow to conceal a
        track. Prediction buys *precision*, never permission to skip coverage.
        Without this the guarantee the spec calls unconditional -- never miss a
        track >= 2 minutes, "from splitting geometry, not from offset trust" --
        silently becomes conditional on offset trust for boundary intervals:
        measured on the synthetic 4-hour set, five whole tracks vanished into
        190s+ boundary intervals that prediction had retired unprobed."""
        return right.mid - left.mid > self.cfg.stride

    def _status(self, left: Evidence, right: Evidence) -> str:
        if self._is_boundary(left, right) and not self._needs_coverage(left, right):
            if self._resolved_by_prediction(left, right) is not None:
                return "resolved"
            if self._capped(left, right):
                return "capped"
        ratio = (right.mid - left.mid) / self._target(left, right)
        return "retired" if ratio <= 1.0 else "active"

    # ---- progress metrics (panel / driver) -------------------------------
    def _active_pairs(self) -> list[tuple[Evidence, Evidence, float]]:
        out = []
        for left, right in self._pairs():
            if self._status(left, right) != "active":
                continue
            if self._plan_for(left, right) is None:
                continue
            out.append((left, right, (right.mid - left.mid) / self._target(left, right)))
        return out

    @property
    def max_ratio(self) -> float:
        return max((ratio for _, _, ratio in self._active_pairs()), default=0.0)

    @property
    def max_boundary_width(self) -> float:
        return max(
            (
                right.mid - left.mid
                for left, right, _ in self._active_pairs()
                if self._is_boundary(left, right)
            ),
            default=0.0,
        )

    def estimated_probes_remaining(self) -> int:
        if not self._evidence:
            return 1 if self.duration > 1.0 else 0
        total = 0
        for left, right, ratio in self._active_pairs():
            width = right.mid - left.mid
            if self._needs_coverage(left, right):
                total += max(1, math.ceil(width / self.cfg.stride) - 1)
            elif self._is_boundary(left, right):
                p_start = self._trusted_start(right.identity)
                if p_start is not None and left.mid < p_start < right.mid:
                    total += 1
                else:
                    total += max(1, math.ceil(math.log2(ratio)))
            else:
                total += max(1, math.ceil(math.log2(ratio)))
        return total

    # ---- contradiction collapse ------------------------------------------
    #
    # The scheduler probes hardest exactly where two tracks meet, so the fold
    # now *sees* Shazam changing its mind inside a transition and, left alone,
    # reports every flip as its own track. Measured on a real 4-hour set: the
    # cut from Boz Scaggs' "Lowdown" into Grant Green's "Sookie Sookie (Live)"
    # folded into four segments spanning 36s, alternating Sookie with Us3's
    # "Tukka Yoot's Riddim" -- Us3 sample Blue Note records and Grant Green is
    # Blue Note, so the confusion is semantic, not noise. Same cause 2.5 hours
    # earlier: a 16s Notorious B.I.G. "Hypnotize" wedged into the cut onto Herb
    # Alpert's "Rise", which "Hypnotize" samples.
    #
    # Extent alone cannot adjudicate this, and that is the whole difficulty.
    # Dropping every pinned run under `phantom_min` convicts all three real
    # phantoms -- and also deletes a genuinely short track: measured against
    # the synthetic oracle, 100% of real 12-18s tracks and ~60% of real 20s
    # ones, every one of which the code before this shipped correctly. So the
    # geometry only ever opens the question; an *offset* veto answers it.

    def _run_estimates(self, run: list[Evidence]) -> list[float]:
        """Each probe in this run's own implied track start (see
        `_probe_start_estimate`), which is a property of the probe, not of the
        cluster -- `_trusted_start` would average a phantom together with the
        real sightings of the same title elsewhere in the recording."""
        out = (self._probe_start_estimate(ev.probe) for ev in run if ev.probe is not None)
        return [x for x in out if x is not None]

    def _misattributed(
        self, runs: list[list[Evidence]], extents: list[float], i: int
    ) -> str | None:
        """Positive evidence that run `i`'s matches are *another track's* audio.

        Two independent readings, both measured on the real set:

        - **incoherent**: the run's own probes disagree about where the track
          started by more than the run is long. Two probes 2.8s apart implying
          starts 49s apart is not a track heard twice, it is two guesses --
          which is exactly what the second "Tukka" run (5.6s extent, 49.0s
          spread) looks like. The bound is the run's own extent because a
          *long* run legitimately spreads: 10 of the real file's 51 multi-probe
          runs spread past 4s, up to 455s over a 462s track, so any fixed
          tolerance either misses the phantom or convicts half the set.
        - **collide**: the run's implied start sits within `offset_tolerance`
          of a neighbouring run's, and that neighbour is better evidenced. Two
          identities cannot both begin at the same instant; one of them is
          hearing the other. Measured: "Hypnotize" implies 3973.3 and "Rise"
          implies 3973.3 -- the same number to 0.1s -- and the first "Tukka"
          run lands 1.4s off Sookie's. Comparing *support* is what keeps the
          test one-directional: the phantom collides with the real track just
          as much as the real track collides with the phantom, and only the
          probe count and extent break the symmetry.

        A run whose probes carry no offsets is never convicted. The rule
        requires evidence; absence of evidence is not it.
        """
        own = self._run_estimates(runs[i])
        if not own:
            return None
        if len(own) > 1 and max(own) - min(own) > extents[i] + self.cfg.offset_tolerance:
            return "incoherent"
        mine = median(own)
        for j in (i - 1, i + 1):
            if not (0 <= j < len(runs)) or runs[j][0].identity is None:
                continue
            theirs = self._run_estimates(runs[j])
            if not theirs or abs(mine - median(theirs)) > self.cfg.offset_tolerance:
                continue
            if (len(runs[j]), extents[j]) > (len(runs[i]), extents[i]):
                return "collide"
        return None

    def _alternation_zones(
        self, runs: list[list[Evidence]], starts: list[float], ends: list[float]
    ) -> list[tuple[int, int, object]]:
        """`(lo, hi, winner)` for every span where two identities take turns.

        This is the spec's own unimplemented rule ("A, then B, then A again
        ... likely a transition zone"), read off the folded runs. A **return**
        is one identity's consecutive pair of runs separated by an excursion no
        longer than `stride` -- the span the coverage guarantee says cannot
        hide a track, so an identity that comes back that fast was never really
        interrupted. One return is A-B-A, the shape the sequential path smoothed
        and `singleton_confidence_keep` still owns; a **zone** needs two
        *overlapping* returns of different identities, which is A-B-A-B: both
        sides come back, and that mutual contradiction is the evidence.

        A `None` run clears every open return: an unidentified stretch between
        two sightings of one track is a dropout, not an alternation.

        Deterministic and total by construction -- returns are collected in run
        order, merged into components by index overlap, and the winner is the
        maximum of `(probes, extent, -first run)`, which no two identities can
        tie on. `segments()` is a pure fold and resume is replay, so anything
        order-dependent here would break `test_replay_equality`.
        """
        returns: list[tuple[int, int, object]] = []
        last: dict[object, int] = {}
        for i, run in enumerate(runs):
            ident = run[0].identity
            if ident is None:
                last.clear()
                continue
            prev = last.get(ident)
            if prev is not None and i - prev >= 2 and starts[i] - ends[prev] <= self.cfg.stride:
                returns.append((prev, i, ident))
            last[ident] = i

        comps: list[list] = []
        for lo, hi, ident in returns:
            if comps and lo <= comps[-1][1]:
                comps[-1][1] = max(comps[-1][1], hi)
                comps[-1][2].append(ident)
            else:
                comps.append([lo, hi, [ident]])

        zones = []
        for lo, hi, idents in comps:
            if len(idents) < 2 or len(set(idents)) < 2:
                continue
            tally: dict[object, tuple[int, float, int]] = {}
            for k in range(lo, hi + 1):
                ident = runs[k][0].identity
                if ident is None:
                    continue
                probes, extent, first = tally.get(ident, (0, 0.0, k))
                tally[ident] = (probes + len(runs[k]), extent + ends[k] - starts[k], first)
            winner = max(tally, key=lambda k: (tally[k][0], tally[k][1], -tally[k][2]))
            zones.append((lo, hi, winner))
        return zones

    def _collapsed(
        self,
        runs: list[list[Evidence]],
        starts: list[float],
        ends: list[float],
        measured: list[bool],
    ) -> dict[int, dict]:
        """Run index -> audit event, for each run the evidence contradicts.

        Three gates, and every one of them earns its place against a measured
        counterexample:

        - **pinned and measured.** Both neighbours identified (a `None` is the
          absence of an answer, not a competing claim -- this is what spares
          the real set's "Our Voyage" and "Deomid", each 12.7s beside an
          unidentified stretch), and both bounding boundaries pinned at least
          as tightly as a probe can pin one. The extent has to be a
          *measurement* before it can contradict anything; a run whose flank is
          still open may genuinely span it, which is the same reason the
          singleton rule refuses to drop one. The tolerance is
          `min(target, fingerprint_segment / 2)` rather than the target alone,
          so raising `--precision` -- a knob for spending fewer probes --
          cannot silently buy more deletions.
        - **too small to be a track.** Under `phantom_min` outright, or under
          `stride` inside an alternation zone: two identities taking turns is
          the licence to distrust a longer run than the bare floor allows.
        - **convicted by its own offsets** (`_misattributed`). Without this the
          rule is a track-length guillotine; with it, the real phantoms are
          convicted and a genuine short track is not. Measured: on the real
          317-probe file it convicts exactly the three phantoms and nothing
          else; against the oracle it acquits 40 of 40 runs in a rewind (the DJ
          pulls a record back, producing a genuine A-B-A-B), where the geometry
          alone would have deleted a real 162s record.

        The zone's own best-supported identity is never collapsed, whatever its
        extent -- on the real file the correct track, Sookie, clears
        `phantom_min` by only 2.8s, which is less than the engine's own p90
        boundary error, so the winner needs protecting by name rather than by
        arithmetic.
        """
        cfg = self.cfg
        extents = [e - s for s, e in zip(starts, ends)]
        winner_at: dict[int, object] = {}
        span_at: dict[int, tuple[int, int]] = {}
        for lo, hi, winner in self._alternation_zones(runs, starts, ends):
            for k in range(lo, hi + 1):
                winner_at[k] = winner
                span_at[k] = (lo, hi)

        out: dict[int, dict] = {}
        for i, run in enumerate(runs):
            ident = run[0].identity
            if ident is None or not (0 < i < len(runs) - 1):
                continue
            if runs[i - 1][0].identity is None or runs[i + 1][0].identity is None:
                continue
            if not (measured[i - 1] and measured[i]):
                continue
            if ident == winner_at.get(i):
                continue
            if i in winner_at and extents[i] < cfg.stride:
                kind = "thrash"
            elif extents[i] < cfg.phantom_min:
                kind = "sliver"
            else:
                continue
            reason = self._misattributed(runs, extents, i)
            if reason is None:
                continue
            meta = self._cluster_meta.get(ident) or {}
            event = {
                "type": "contradiction_collapsed",
                "kind": kind,
                "reason": reason,
                "artist": meta.get("artist"),
                "title": meta.get("title"),
                "start": round(starts[i], 1),
                "extent": round(extents[i], 1),
            }
            if i in winner_at:
                wmeta = self._cluster_meta.get(winner_at[i]) or {}
                event["winner_artist"] = wmeta.get("artist")
                event["winner_title"] = wmeta.get("title")
                event["zone"] = [round(starts[span_at[i][0]], 1), round(ends[span_at[i][1]], 1)]
            out[i] = event
        return out

    # ---- finalization ----------------------------------------------------
    def _runs(self) -> list[list[Evidence]]:
        runs: list[list[Evidence]] = []
        for ev in self._evidence:
            if runs and runs[-1][-1].identity == ev.identity:
                runs[-1].append(ev)
            else:
                runs.append([ev])
        return runs

    def segments(self) -> tuple[list[Segment], list[dict]]:
        """Fold the evidence into a tracklist. Callable at ANY point -- this is
        what makes every stopping rule (converged, budget, Ctrl-C) the same
        code path. Returns (segments, phantom_dropped audit events)."""
        cfg = self.cfg
        runs = self._runs()
        if not runs:
            return ([Segment(0.0, None, "coarse")] if self.duration > 0 else [], [])

        # Boundary between run i and i+1, with its confidence. `measured` is a
        # stricter, precision-independent read of the same question -- see
        # `_collapsed`, which may only act on a boundary a probe could actually
        # have pinned.
        bounds: list[tuple[float, str]] = []
        measured: list[bool] = []
        for a, b in zip(runs, runs[1:]):
            left, right = a[-1], b[0]
            p_start = self._resolved_by_prediction(left, right)
            gap = right.mid - left.mid
            if p_start is not None:
                bounds.append((p_start, "resolved"))
            else:
                conf = "resolved" if gap <= self._target(left, right) else "coarse"
                bounds.append(((left.mid + right.mid) / 2.0, conf))
            measured.append(
                p_start is not None
                or gap <= min(self._target(left, right), cfg.fingerprint_segment / 2.0)
            )

        starts = [0.0] + [b for b, _ in bounds]
        ends = [b for b, _ in bounds] + [self.duration]
        collapsed = self._collapsed(runs, starts, ends, measured)

        # Phantom filtering. Confidence read from the run's own probe, not its
        # cluster -- same reasoning as _smooth_sequence's gate (#7).
        keep: list[int] = []
        drops: list[dict] = []
        for i, run in enumerate(runs):
            ident = run[0].identity
            span = ends[i] - starts[i]
            if i in collapsed:
                drops.append(collapsed[i])
                continue
            if ident is None:
                # A gap between two runs of ONE identity is a dropout inside
                # that track, not a boundary onto silence -- the shape
                # `identify._smooth_sequence` absorbed unconditionally. It is
                # judged at `precision_none` because that is where `_target`
                # stops refining a None-adjacent interval: below it, a dropout
                # that retired in (phantom_min, precision_none] could never be
                # narrowed by any later probe, and the real set reported one
                # track as three rows around a permanent 22.5s hole.
                # `_needs_coverage` still splits everything wider than the
                # stride, so nothing >= 2 minutes can hide in the wider floor.
                inside_one_track = (
                    0 < i < len(runs) - 1
                    and runs[i - 1][0].identity is not None
                    and runs[i - 1][0].identity == runs[i + 1][0].identity
                )
                floor = cfg.precision_none if inside_one_track else cfg.phantom_min
                if span < floor:
                    drops.append(
                        {
                            "type": "phantom_dropped",
                            "kind": "dropout" if inside_one_track else "gap",
                            "start": round(starts[i], 1),
                            "extent": round(span, 1),
                        }
                    )
                    continue
            if ident is not None and len(run) == 1 and span < cfg.phantom_min:
                conf = (run[0].probe.result or {}).get("confidence") or 0
                if conf < cfg.singleton_confidence_keep:
                    meta = self._cluster_meta.get(ident) or {}
                    drops.append(
                        {
                            "type": "phantom_dropped",
                            "kind": "track",
                            "artist": meta.get("artist"),
                            "title": meta.get("title"),
                            "start": round(starts[i], 1),
                            "extent": round(span, 1),
                        }
                    )
                    continue
            keep.append(i)

        out: list[Segment] = []
        prev_kept: int | None = None
        for i in keep:
            ident = runs[i][0].identity
            info = self._cluster_meta.get(ident) if ident is not None else None
            if prev_kept is None:
                out.append(Segment(0.0, info, "resolved"))
            elif ident == runs[prev_kept][0].identity:
                pass  # same track continues across a dropped blip
            elif i == prev_kept + 1:
                b, conf = bounds[prev_kept]
                out.append(Segment(b, info, conf))
            else:
                # Dropped run(s) in between: boundary at the dropped span's center.
                b = (bounds[prev_kept][0] + bounds[i - 1][0]) / 2.0
                out.append(Segment(b, info, "coarse"))
            prev_kept = i

        if not out:
            out = [Segment(0.0, None, "coarse")]
        return out, drops

    def boundary_stats(self) -> tuple[int, int]:
        """(boundaries between distinct identified runs, of those at target)."""
        runs = self._runs()
        found = at_target = 0
        for a, b in zip(runs, runs[1:]):
            left, right = a[-1], b[0]
            if not self._is_boundary(left, right):
                continue
            found += 1
            if self._resolved_by_prediction(
                left, right
            ) is not None or right.mid - left.mid <= self._target(left, right):
                at_target += 1
        return found, at_target
