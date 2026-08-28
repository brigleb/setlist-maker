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

        # Boundary between run i and i+1, with its confidence.
        bounds: list[tuple[float, str]] = []
        for a, b in zip(runs, runs[1:]):
            left, right = a[-1], b[0]
            p_start = self._resolved_by_prediction(left, right)
            if p_start is not None:
                bounds.append((p_start, "resolved"))
            else:
                gap = right.mid - left.mid
                conf = "resolved" if gap <= self._target(left, right) else "coarse"
                bounds.append(((left.mid + right.mid) / 2.0, conf))

        starts = [0.0] + [b for b, _ in bounds]
        ends = [b for b, _ in bounds] + [self.duration]

        # Phantom filtering. Confidence read from the run's own probe, not its
        # cluster -- same reasoning as _smooth_sequence's gate (#7).
        keep: list[int] = []
        drops: list[dict] = []
        for i, run in enumerate(runs):
            ident = run[0].identity
            span = ends[i] - starts[i]
            if ident is None and span < cfg.phantom_min:
                drops.append(
                    {
                        "type": "phantom_dropped",
                        "kind": "gap",
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
