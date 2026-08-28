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
    offset_tolerance: float = 4.0  # max spread among a track's T-O estimates
    timeskew_max: float = 0.02  # beyond this the playback was tempo-shifted
    min_corroboration: int = 2  # probes needed before offsets are trusted
    verify_lead: float = 2.0  # verification probe starts this far after P
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
        """Fold one completed probe into the model. Returns events."""
        identity = self._identify(probe.result)
        ev = Evidence(mid=probe.mid, identity=identity, probe=probe)
        mids = [e.mid for e in self._evidence]
        self._evidence.insert(bisect.bisect_left(mids, ev.mid), ev)
        self.probes.append(probe)
        self._identity_by_index.append(identity)
        return []

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
    def _probe_start_estimate(self, probe: Probe) -> float | None:
        """This probe's implied track start (T - O). A *lower bound*: a track
        the DJ cut into mid-song implies a start earlier than the real
        boundary, which is why prediction is verified after P, never before
        (see spec: Verification protocol)."""
        if not probe.offsets:
            return None
        cands = [
            probe.t - m["offset"]
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
        some B probe *started* within [P - 0.5, P + precision] -- i.e. B was
        confirmed playing just after its predicted start. The verification
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
            if ident == key and p_start - 0.5 <= p.t <= p_start + self.cfg.precision:
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
            if self._is_boundary(left, right):
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
        if self._is_boundary(left, right):
            p_start = self._trusted_start(right.identity)
            if p_start is not None and left.mid < p_start < right.mid:
                t = p_start + cfg.verify_lead
                if t + cfg.refine_window / 2.0 < right.mid - 0.25 and not self._near_existing(t):
                    return ProbePlan(t=t, window=cfg.refine_window, purpose="refine")
            window, purpose = cfg.refine_window, "refine"
            mid = (left.mid + right.mid) / 2.0
        elif self._target(left, right) == cfg.stride:
            window, purpose = cfg.coverage_window, "coverage"
            mid = self._grid_mid(left, right)
        else:  # None-adjacent: hunting identity, not a boundary
            window = cfg.coverage_window if width > cfg.coverage_window * 1.5 else cfg.refine_window
            purpose = "coverage"
            mid = (left.mid + right.mid) / 2.0

        t = mid - window / 2.0
        t = min(max(t, 0.0), max(0.0, self.duration - window))
        if not (left.mid + 0.25 < t + window / 2.0 < right.mid - 0.25):
            return None  # unprobeable sliver (edge clamping pushed us out)
        if self._near_existing(t):
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

    def _near_existing(self, t: float) -> bool:
        return any(abs(p.t - t) < 0.5 for p in self.probes)

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

    def _status(self, left: Evidence, right: Evidence) -> str:
        if self._is_boundary(left, right):
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
            if self._is_boundary(left, right):
                p_start = self._trusted_start(right.identity)
                if p_start is not None and left.mid < p_start < right.mid:
                    total += 1
                else:
                    total += max(1, math.ceil(math.log2(ratio)))
            elif self._target(left, right) == self.cfg.stride:
                total += max(1, math.ceil(width / self.cfg.stride) - 1)
            else:
                total += max(1, math.ceil(math.log2(ratio)))
        return total
