# Adaptive Boundary Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the sequential 30s identify scan with an anytime adaptive sampling engine (priority-queue interval refinement + Shazam match-offset prediction) as the default, with `--sequential` preserving the old path.

**Architecture:** A pure engine module (`boundary.py`) folds completed probes into an interval model and answers "what to probe next"; an impure driver (`adaptive.py`) owns Shazam, delays, persistence, signals, and the panel. State is a deterministic fold over the probe list, so resume is replay. Sequential code in `identify.py` is untouched except for two shared helpers.

**Tech Stack:** Python 3.10+, stdlib (`statistics`, `bisect`, `math`, `signal`, `json`), existing deps only (pydub, shazamio, rich). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-27-adaptive-boundary-detection-design.md`

## Global Constraints

- Python 3.10+; ruff rules E, F, W, I; line length 100 (`pyproject.toml`).
- Conventional commits for every commit.
- No new runtime dependencies.
- Tests must never touch the network: `tests/conftest.py` has an autouse guard raising `NetworkAccessBlocked` (a `BaseException`). Patch seams in the **importing module's namespace** (e.g. `setlist_maker.adaptive.identify_sample_with_retry`, not `setlist_maker.shazam_client...`).
- The live panel's height must never change between renders (`Live` erases a fixed number of lines). Every glyph one cell wide; every field through `_one_line()`-style collapsing.
- The JSON sidecar stays a bare list; output file formats do not change.
- Run `pytest` and `ruff check .` before every commit.

## File Structure

- **Create `setlist_maker/boundary.py`** — pure engine: `Probe`, `ProbePlan`, `EngineConfig`, `Evidence`, `Segment`, `BoundaryEngine` (fold, scheduler, prediction, segments, events). No I/O, no clock, no network.
- **Create `setlist_maker/adaptive.py`** — driver: `process_single_file_adaptive()`, progress-v2 save/load, `EventLog`, `format_probe_line()`, SIGINT flag.
- **Create `scripts/offset_spike.py`** — throwaway empirical offset check (spec step one).
- **Modify `setlist_maker/shazam_client.py`** — optional `include_offsets` on `identify_sample_with_retry`.
- **Modify `setlist_maker/audio.py`** — add `extract_window()`.
- **Modify `setlist_maker/identify.py`** — `results_to_tracklist(deduplicate=...)`, extract `finalize_outputs()`, guard sequential resume against v2 progress files.
- **Modify `setlist_maker/progress.py`** — `AdaptiveRunState`, `render_adaptive_panel()`, `render` parameter on `ProgressPanel`/`live_display`.
- **Modify `setlist_maker/cli.py`** — `--sequential`, `--precision`, `--budget`, `--stride`, `--refine-window`, `parse_budget()`, routing, epilog.
- **Create tests:** `tests/test_boundary_engine.py`, `tests/test_boundary_segments.py`, `tests/boundary_oracle.py`, `tests/test_boundary_properties.py`, `tests/test_adaptive_progress.py`, `tests/test_adaptive_driver.py`, `tests/test_adaptive_panel.py`, `tests/test_cli_adaptive.py`, plus small additions to existing test files where noted.

Design deviations locked in here (record in spec errata in Task 15): the per-boundary probe cap is implemented as a **per-coverage-gap refine cap** (`max_refines_per_gap = 12`) because every probe becomes an evidence point that splits its interval, so "probes inside an interval" is always zero; coverage probes snap to a stride grid (`_grid_mid`) so full coverage costs `duration/stride` probes instead of the up-to-2× overshoot of blind midpoint halving.

---

### Task 1: Offset capture in shazam_client

**Files:**
- Modify: `setlist_maker/shazam_client.py`
- Test: `tests/test_shazam_offsets.py` (create)

**Interfaces:**
- Consumes: existing `identify_sample_with_retry(shazam, segment, temp_dir, max_retries=..., on_backoff=...)`.
- Produces: `identify_sample_with_retry(..., include_offsets: bool = False)` — when True, the returned info dict gains `"offsets": [{"offset": float, "timeskew": float|None}, ...]` built from the raw result's top-level `matches` list (same source `estimate_confidence` reads). When False (default), the return value is byte-identical to today.

- [ ] **Step 1: Write the failing test**

```python
"""Offset capture on identify_sample_with_retry (adaptive engine's raw material)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from setlist_maker.shazam_client import identify_sample_with_retry

RAW = {
    "matches": [{"id": "1", "offset": 154.2, "timeskew": 0.0007, "frequencyskew": 0.001}],
    "track": {"title": "One More Time", "subtitle": "Daft Punk", "url": "u", "images": {}},
}


def _fake_shazam(raw):
    shazam = MagicMock()
    shazam.recognize = AsyncMock(return_value=raw)
    return shazam


def _segment():
    seg = MagicMock()
    seg.export = MagicMock()
    return seg


def test_offsets_included_when_requested(tmp_path):
    info = asyncio.run(
        identify_sample_with_retry(
            _fake_shazam(RAW), _segment(), str(tmp_path), include_offsets=True
        )
    )
    assert info["offsets"] == [{"offset": 154.2, "timeskew": 0.0007}]


def test_offsets_absent_by_default(tmp_path):
    info = asyncio.run(
        identify_sample_with_retry(_fake_shazam(RAW), _segment(), str(tmp_path))
    )
    assert "offsets" not in info


def test_matches_without_offset_are_skipped(tmp_path):
    raw = {"matches": [{"id": "1"}], "track": RAW["track"]}
    info = asyncio.run(
        identify_sample_with_retry(
            _fake_shazam(raw), _segment(), str(tmp_path), include_offsets=True
        )
    )
    assert info["offsets"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shazam_offsets.py -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'include_offsets'`

- [ ] **Step 3: Implement**

In `identify_sample_with_retry`, add the parameter `include_offsets: bool = False` (after `max_retries`, before `on_backoff`). In the success branch (where the info dict is built and returned), change to:

```python
                info = {
                    "title": track.get("title", "Unknown Title"),
                    "artist": track.get("subtitle", "Unknown Artist"),
                    "shazam_url": track.get("url"),
                    "album": track.get("sections", [{}])[0].get("metadata", [{}])[0].get("text")
                    if track.get("sections")
                    else None,
                    "coverart_url": images.get("coverarthq") or images.get("coverart"),
                    "confidence": estimate_confidence(result),
                }
                if include_offsets:
                    # Raw material for adaptive boundary prediction: where within
                    # the matched song this sample aligned. Opt-in so the
                    # sequential path's progress files keep their exact shape.
                    info["offsets"] = [
                        {"offset": m.get("offset"), "timeskew": m.get("timeskew")}
                        for m in (result.get("matches") or [])
                        if isinstance(m.get("offset"), (int, float))
                    ]
                return info
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shazam_offsets.py -v` — Expected: 3 PASS. Then `pytest` (full suite) — expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/shazam_client.py tests/test_shazam_offsets.py
git commit -m "feat(shazam): optionally return match offsets for boundary prediction"
```

---

### Task 2: extract_window in audio.py

**Files:**
- Modify: `setlist_maker/audio.py`
- Test: `tests/test_audio_extract_window.py` (create)

**Interfaces:**
- Produces: `extract_window(audio: AudioSegment, start_seconds: float, window_seconds: float) -> AudioSegment` — the [start, start+window) slice, clamped to the audio's bounds.

- [ ] **Step 1: Write the failing test**

```python
"""extract_window: arbitrary-position slicing for adaptive probes."""

from pydub import AudioSegment

from setlist_maker.audio import extract_window


def test_extracts_requested_window():
    audio = AudioSegment.silent(duration=60_000)  # 60s
    seg = extract_window(audio, 10.0, 12.0)
    assert len(seg) == 12_000


def test_clamps_at_end_of_audio():
    audio = AudioSegment.silent(duration=60_000)
    seg = extract_window(audio, 55.0, 12.0)
    assert len(seg) == 5_000


def test_clamps_negative_start():
    audio = AudioSegment.silent(duration=60_000)
    seg = extract_window(audio, -3.0, 12.0)
    assert len(seg) == 12_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_audio_extract_window.py -v`
Expected: FAIL with `ImportError: cannot import name 'extract_window'`

- [ ] **Step 3: Implement** (place after `slice_audio` in `audio.py`)

```python
def extract_window(
    audio: AudioSegment, start_seconds: float, window_seconds: float
) -> AudioSegment:
    """Slice one probe window from anywhere in the recording.

    The adaptive engine plans probes at arbitrary (float-second) positions;
    this is its counterpart to `slice_audio`'s fixed grid. Clamped to the
    audio's bounds, so a window planned near the end simply comes back short.
    """
    start_ms = max(0, int(round(start_seconds * 1000)))
    end_ms = min(len(audio), start_ms + int(round(window_seconds * 1000)))
    return audio[start_ms:end_ms]
```

- [ ] **Step 4: Run tests** — `pytest tests/test_audio_extract_window.py -v` → 3 PASS; full `pytest` green.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/audio.py tests/test_audio_extract_window.py
git commit -m "feat(audio): add extract_window for arbitrary-position probes"
```

---

### Task 3: Offset spike script (spec step one)

**Files:**
- Create: `scripts/offset_spike.py`

**Interfaces:**
- Consumes: `load_audio`, `extract_window` (Task 2), `identify_sample_with_retry(include_offsets=True)` (Task 1).
- Produces: a throwaway CLI report; findings go into the spec's Errata section and may adjust `EngineConfig.min_corroboration` (Task 4) from its safe default of 2.

- [ ] **Step 1: Write the script** (no test — it is a manual, network-using probe tool; the tests/ network guard does not apply because it lives outside pytest)

```python
#!/usr/bin/env python3
"""Throwaway empirical check of Shazam match offsets (design-spec step one).

Probes a real recording at several positions and prints, for each probe at
time T matching offset O, the implied track start T - O. Within one
continuously-played track those values should agree to within a few seconds;
across a hard cut they should jump. Run:

    python scripts/offset_spike.py recording.mp3            # 8 spread probes
    python scripts/offset_spike.py recording.mp3 300 330 360 1200

Findings land in docs/superpowers/specs/2026-08-27-adaptive-boundary-detection-design.md
(Errata): offset sign/meaning, T-O consistency, behavior across a cut, and
12s-vs-30s window match rates.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shazamio import Shazam  # noqa: E402

from setlist_maker.audio import extract_window, format_timestamp, load_audio  # noqa: E402
from setlist_maker.shazam_client import identify_sample_with_retry  # noqa: E402

DELAY = 15
WINDOWS = (30.0, 12.0)


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    audio = load_audio(Path(sys.argv[1]))
    duration = len(audio) / 1000.0
    if len(sys.argv) > 2:
        positions = [float(a) for a in sys.argv[2:]]
    else:
        positions = [duration * (i + 1) / 9 for i in range(8)]

    shazam = Shazam()
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        for t in positions:
            for window in WINDOWS:
                seg = extract_window(audio, t, window)
                info = await identify_sample_with_retry(
                    shazam, seg, temp_dir, include_offsets=True
                )
                stamp = format_timestamp(int(t))
                if not info:
                    print(f"{stamp}  w={window:>4.0f}s  -- no match")
                else:
                    offsets = info.get("offsets") or []
                    implied = [f"{format_timestamp(int(t - m['offset']))}" for m in offsets]
                    print(
                        f"{stamp}  w={window:>4.0f}s  {info['artist']} - {info['title']}  "
                        f"offsets={[round(m['offset'], 1) for m in offsets]}  "
                        f"timeskew={[m.get('timeskew') for m in offsets]}  "
                        f"implied start(s)={implied}"
                    )
                await asyncio.sleep(DELAY)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it if a real recording is at hand.** Ask the human partner for a recording path (any file previously processed by setlist-maker is ideal — its tracklist is ground truth). If none is available in this environment, **skip the run**, keep `min_corroboration = 2` (partial trust, the safe default), and add an Errata line: "Spike deferred — shipping partial-trust default (`min_corroboration=2`); rerun `scripts/offset_spike.py` on a real set before promoting to 1."

- [ ] **Step 3: Record findings** in the spec's Errata section (offset sign, T−O consistency spread, cut behavior, 12s window match rate) and adjust the Task 4 default for `min_corroboration` if the evidence supports full trust (1) or demands distrust (raise `offset_tolerance`, keep 2).

- [ ] **Step 4: Commit**

```bash
git add scripts/offset_spike.py docs/superpowers/specs/2026-08-27-adaptive-boundary-detection-design.md
git commit -m "feat(spike): empirical Shazam offset validation script + errata"
```

---

### Task 4: boundary.py — core data model and fold

**Files:**
- Create: `setlist_maker/boundary.py`
- Test: `tests/test_boundary_engine.py` (create)

**Interfaces:**
- Consumes: `_assign_cluster`, `_normalized_key` from `setlist_maker.identify` (existing fuzzy-clustering helpers).
- Produces (used by every later task):
  - `Probe(t: float, window: float, purpose: str, result: dict | None, offsets: list[dict] | None = None)`, frozen, with `.mid` property.
  - `ProbePlan(t: float, window: float, purpose: str)`, frozen.
  - `EngineConfig` dataclass (defaults below).
  - `Evidence(mid: float, identity: object, probe: Probe | None)`, frozen.
  - `Segment(start: float, info: dict | None, confidence: str)`, frozen.
  - `START`, `END` sentinels.
  - `BoundaryEngine(duration: float, config: EngineConfig | None = None)` with `.probes`, `.cfg`, `.duration`, `.add_probe(probe) -> list[dict]` (returns `[]` until Task 8), `._points()`, `._pairs()`, `._target(left, right) -> float`, `._is_boundary(left, right) -> bool`, `._enclosing(mid) -> tuple[Evidence, Evidence] | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""BoundaryEngine core: identity clustering, evidence ordering, interval targets."""

from setlist_maker.boundary import (
    END,
    START,
    BoundaryEngine,
    EngineConfig,
    Probe,
)


def probe(t, artist=None, title=None, window=30.0, purpose="coverage",
          confidence=0.9, offsets=None):
    result = None
    if title is not None:
        result = {"artist": artist or "X", "title": title, "confidence": confidence}
    return Probe(t=t, window=window, purpose=purpose, result=result, offsets=offsets)


def test_probe_mid():
    assert probe(100.0).mid == 115.0


def test_evidence_sorted_and_virtual_endpoints():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(300.0, title="B"))
    eng.add_probe(probe(60.0, title="A"))
    points = eng._points()
    assert points[0].identity is START and points[0].mid == 0.0
    assert points[-1].identity is END and points[-1].mid == 600.0
    assert [p.mid for p in points[1:-1]] == [75.0, 315.0]


def test_fuzzy_identity_collapses_metadata_drift():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(60.0, artist="Daft Punk", title="One More Time"))
    eng.add_probe(probe(150.0, artist="Daft Punk", title="One More Time (Radio Edit)"))
    ids = eng._identity_by_index
    assert ids[0] == ids[1]


def test_none_result_is_none_identity():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(60.0))
    assert eng._identity_by_index == [None]


def test_targets_by_interval_kind():
    cfg = EngineConfig()
    eng = BoundaryEngine(1000.0, cfg)
    eng.add_probe(probe(100.0, title="A"))
    eng.add_probe(probe(300.0, title="A"))
    eng.add_probe(probe(500.0, title="B"))
    eng.add_probe(probe(700.0))  # miss
    pts = eng._points()
    # START..A edge, A..A same-track -> stride; A..B boundary -> precision;
    # B..None -> precision_none; None..END edge -> stride.
    targets = [eng._target(left, right) for left, right in zip(pts, pts[1:])]
    assert targets == [cfg.stride, cfg.stride, cfg.precision, cfg.precision_none, cfg.stride]


def test_is_boundary_only_for_two_distinct_real_tracks():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(100.0, title="A"))
    eng.add_probe(probe(300.0, title="B"))
    eng.add_probe(probe(500.0))
    pts = eng._points()
    flags = [eng._is_boundary(left, right) for left, right in zip(pts, pts[1:])]
    assert flags == [False, True, False, False]


def test_cluster_meta_keeps_highest_confidence_variant():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(60.0, title="One More Time (Radio Edit)", confidence=0.5))
    eng.add_probe(probe(150.0, title="One More Time", confidence=0.9))
    key = eng._identity_by_index[0]
    assert eng._cluster_meta[key]["title"] == "One More Time"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_boundary_engine.py -v` — Expected: FAIL with `ModuleNotFoundError: No module named 'setlist_maker.boundary'`

- [ ] **Step 3: Implement `setlist_maker/boundary.py`**

```python
"""Adaptive boundary detection engine.

Pure: no I/O, no clock, no network. The engine consumes completed `Probe`s and
answers "what should be probed next?" (`next_probe`, Task 6) and "what does the
evidence say the recording contains?" (`segments`, Task 7). All state is a
deterministic fold over the probe sequence -- replaying the same probes in the
same order rebuilds the identical engine, which is what makes resume "load the
probe list and replay it" (see the design spec).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

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
        """Fold one completed probe into the model. Returns events (Task 8)."""
        identity = self._identify(probe.result)
        ev = Evidence(mid=probe.mid, identity=identity, probe=probe)
        mids = [e.mid for e in self._evidence]
        self._evidence.insert(bisect.bisect_left(mids, ev.mid), ev)
        self.probes.append(probe)
        self._identity_by_index.append(identity)
        return []

    # ---- interval model --------------------------------------------------
    def _points(self) -> list[Evidence]:
        return (
            [Evidence(0.0, START, None)]
            + self._evidence
            + [Evidence(self.duration, END, None)]
        )

    def _pairs(self) -> list[tuple[Evidence, Evidence]]:
        pts = self._points()
        return list(zip(pts, pts[1:]))

    def _target(self, left: Evidence, right: Evidence) -> float:
        li, ri = left.identity, right.identity
        if li is None or ri is None:
            # An unidentified stretch: refine to roughly today's sequential
            # granularity, no finer -- Shazam already failed here once.
            return self.cfg.precision_none
        if li is START or ri is END or li == ri:
            return self.cfg.stride
        return self.cfg.precision

    def _is_boundary(self, left: Evidence, right: Evidence) -> bool:
        li, ri = left.identity, right.identity
        return (
            li not in (None, START, END)
            and ri not in (None, START, END)
            and li != ri
        )

    def _enclosing(self, mid: float) -> tuple[Evidence, Evidence] | None:
        for left, right in self._pairs():
            if left.mid <= mid < right.mid:
                return (left, right)
        return None
```

- [ ] **Step 4: Run tests** — `pytest tests/test_boundary_engine.py -v` → all PASS; full `pytest` green; `ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/boundary.py tests/test_boundary_engine.py
git commit -m "feat(boundary): core probe fold, identity clustering, interval model"
```

---

### Task 5: boundary.py — offset prediction

**Files:**
- Modify: `setlist_maker/boundary.py`
- Test: `tests/test_boundary_engine.py` (append)

**Interfaces:**
- Consumes: Task 4's model.
- Produces: `BoundaryEngine._probe_start_estimate(probe) -> float | None`, `._trusted_start(key) -> float | None`, `._resolved_by_prediction(left, right) -> float | None`. Semantics per spec: `T − O` is a **lower bound** on the track's start; trusted only with `min_corroboration` estimates agreeing within `offset_tolerance` and small `timeskew`; an `A…B` interval is resolved when trusted `P` lies inside it **and** some B-probe *started* within `[P − 0.5, P + precision]` (the after-P verification, a pure predicate over the probe set — no plan memory).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_boundary_engine.py`)

```python
def off(o, skew=0.0):
    return {"offset": o, "timeskew": skew}


def test_probe_start_estimate_is_t_minus_offset():
    eng = BoundaryEngine(1000.0)
    p = probe(200.0, title="B", offsets=[off(50.0)])
    assert eng._probe_start_estimate(p) == 150.0


def test_probe_start_estimate_ignores_timeskewed_matches():
    eng = BoundaryEngine(1000.0)
    p = probe(200.0, title="B", offsets=[off(50.0, skew=0.5)])
    assert eng._probe_start_estimate(p) is None


def test_trusted_start_needs_corroboration_and_agreement():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(200.0, title="B", offsets=[off(50.0)]))
    key = eng._identity_by_index[0]
    assert eng._trusted_start(key) is None  # min_corroboration = 2
    eng.add_probe(probe(290.0, title="B", offsets=[off(141.0)]))
    assert eng._trusted_start(key) == 149.5  # median of 150.0, 149.0
    eng.add_probe(probe(380.0, title="B", offsets=[off(100.0)]))  # wildly off
    assert eng._trusted_start(key) is None  # spread > offset_tolerance


def test_resolved_by_prediction_requires_confirming_probe_after_p():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    eng.add_probe(probe(200.0, title="B", offsets=[off(50.0)]))
    eng.add_probe(probe(290.0, title="B", offsets=[off(140.0)]))
    pts = eng._points()
    boundary = next(
        (l, r) for l, r in zip(pts, pts[1:]) if eng._is_boundary(l, r)
    )
    # P = 150, inside (75, 215); but no B probe *starts* within [149.5, 155].
    assert eng._resolved_by_prediction(*boundary) is None
    eng.add_probe(probe(152.0, title="B", window=12.0, purpose="refine",
                        offsets=[off(2.0)]))
    pts = eng._points()
    boundary = next(
        (l, r) for l, r in zip(pts, pts[1:]) if eng._is_boundary(l, r)
    )
    resolved = eng._resolved_by_prediction(*boundary)
    assert resolved is not None and abs(resolved - 150.0) < 1.0
```

- [ ] **Step 2: Run** — `pytest tests/test_boundary_engine.py -v` — Expected: new tests FAIL with `AttributeError: ... has no attribute '_probe_start_estimate'`

- [ ] **Step 3: Implement** (add to `BoundaryEngine`; add `from statistics import median` to imports)

```python
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
```

- [ ] **Step 4: Run tests** — all PASS; full suite green; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/boundary.py tests/test_boundary_engine.py
git commit -m "feat(boundary): offset-based start prediction with trust gating"
```

---

### Task 6: boundary.py — the scheduler (next_probe)

**Files:**
- Modify: `setlist_maker/boundary.py`
- Test: `tests/test_boundary_engine.py` (append)

**Interfaces:**
- Consumes: Tasks 4–5.
- Produces: `BoundaryEngine.next_probe() -> ProbePlan | None` (None = converged), `._plan_for(left, right) -> ProbePlan | None`, `._grid_mid(left, right) -> float`, `._near_existing(t) -> bool`, `._coverage_gap(left, right) -> tuple[float, float]`, `._capped(left, right) -> bool`, `._status(left, right) -> str` (`"active" | "resolved" | "capped" | "retired"`), `.max_ratio: float` property, `.max_boundary_width: float` property, `.estimated_probes_remaining() -> int`.

Scheduler rules (from spec, plus locked deviations):
- Priority = `width / target`; pop the highest ratio > 1; ties keep the earliest interval (iteration order).
- Skip intervals that are resolved-by-prediction, thrash-capped, or unprobeable (planned mid would fall outside the interval after edge clamping, or a probe already exists within 0.5s of the planned `t`).
- Boundary intervals with a trusted P strictly inside plan a **verification** probe at `t = P + verify_lead`, window `refine_window`, purpose `"refine"` — only if that probe's midpoint still lands strictly inside the interval; otherwise fall through to bisection at the midpoint with `refine_window`.
- Stride-target intervals probe at `_grid_mid` (nearest stride multiple strictly inside; plain midpoint as fallback) with `coverage_window`, purpose `"coverage"` — this makes full coverage land exactly on the 90s grid.
- Other intervals (None-adjacent) bisect at the plain midpoint with `coverage_window` if the interval is wide (> 1.5 × coverage_window) else `refine_window`, purpose `"coverage"` (they hunt identity, not a boundary).
- Bootstrap: with zero real evidence and `duration > coverage_window`, always plan one coverage probe centered on the file, regardless of ratio (a 60s file must still get one probe).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_boundary_engine.py`)

```python
def drive(eng, answer, max_probes=500):
    """Loop next_probe -> add_probe, answering via `answer(t, window)`."""
    n = 0
    while (plan := eng.next_probe()) is not None:
        assert n < max_probes, "scheduler failed to converge"
        result, offsets = answer(plan.t, plan.window)
        eng.add_probe(Probe(t=plan.t, window=plan.window, purpose=plan.purpose,
                            result=result, offsets=offsets))
        n += 1
    return n


def test_first_probe_is_center_of_file():
    eng = BoundaryEngine(14400.0)
    plan = eng.next_probe()
    assert plan.purpose == "coverage"
    assert abs((plan.t + plan.window / 2) - 7200.0) < 45.0  # near center, on grid


def test_tiny_file_still_gets_one_probe():
    eng = BoundaryEngine(60.0)
    plan = eng.next_probe()
    assert plan is not None and plan.purpose == "coverage"
    eng.add_probe(Probe(plan.t, plan.window, plan.purpose, None, None))
    assert eng.next_probe() is None


def test_coverage_lands_on_stride_grid_and_costs_duration_over_stride():
    eng = BoundaryEngine(1800.0)  # 30 min, one track everywhere

    def answer(t, w):
        return ({"artist": "X", "title": "A", "confidence": 0.9}, None)

    n = drive(eng, answer)
    # duration/stride = 20 intervals -> 19 interior grid points, +-2 for edges
    assert n <= 22
    mids = sorted(p.mid for p in eng.probes)
    gaps = [b - a for a, b in zip(mids, mids[1:])]
    assert max(gaps) <= eng.cfg.stride + 0.5
    assert all(g > 1.0 for g in gaps)


def test_boundary_refined_to_precision_without_offsets():
    eng = BoundaryEngine(600.0)
    true_boundary = 293.0

    def answer(t, w):
        mid = t + w / 2
        title = "A" if mid < true_boundary else "B"
        return ({"artist": "X", "title": title, "confidence": 0.9}, None)

    drive(eng, answer)
    pts = eng._points()
    boundary = next((l, r) for l, r in zip(pts, pts[1:]) if eng._is_boundary(l, r))
    assert boundary[1].mid - boundary[0].mid <= eng.cfg.precision + 0.01


def test_trusted_prediction_plans_verification_after_p():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    eng.add_probe(probe(300.0, title="B", offsets=[off(150.0)]))
    eng.add_probe(probe(390.0, title="B", offsets=[off(240.0)]))
    # P = 150 inside (75, 315): expect verify at P + verify_lead with refine window.
    plan = eng.next_probe()
    assert plan.purpose == "refine"
    assert abs(plan.t - (150.0 + eng.cfg.verify_lead)) < 0.01
    assert plan.window == eng.cfg.refine_window


def test_converges_even_when_oracle_is_noisy_at_boundary():
    import random

    rng = random.Random(7)
    eng = BoundaryEngine(600.0)
    true_boundary = 300.0

    def answer(t, w):
        mid = t + w / 2
        if abs(mid - true_boundary) < w / 2:  # window straddles: coin flip
            title = rng.choice(["A", "B"])
        else:
            title = "A" if mid < true_boundary else "B"
        return ({"artist": "X", "title": title, "confidence": 0.9}, None)

    n = drive(eng, answer)
    assert n < 120  # bounded spend, no livelock
    assert eng.next_probe() is None
```

- [ ] **Step 2: Run** — Expected: FAIL with `AttributeError: ... no attribute 'next_probe'`

- [ ] **Step 3: Implement** (add to `BoundaryEngine`; add `import math` to imports)

```python
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
                if (
                    t + cfg.refine_window / 2.0 < right.mid - 0.25
                    and not self._near_existing(t)
                ):
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
        return max((r for _, _, r in self._active_pairs()), default=0.0)

    @property
    def max_boundary_width(self) -> float:
        return max(
            (r.mid - l.mid for l, r, _ in self._active_pairs() if self._is_boundary(l, r)),
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
```

- [ ] **Step 4: Run tests** — all PASS; full suite green; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/boundary.py tests/test_boundary_engine.py
git commit -m "feat(boundary): anytime priority scheduler with verification and grid coverage"
```

---

### Task 7: boundary.py — segments() fold and boundary_stats()

**Files:**
- Modify: `setlist_maker/boundary.py`
- Test: `tests/test_boundary_segments.py` (create)

**Interfaces:**
- Consumes: Tasks 4–6.
- Produces: `BoundaryEngine.segments() -> tuple[list[Segment], list[dict]]` (segments plus `phantom_dropped` audit dicts) and `.boundary_stats() -> tuple[int, int]` (boundaries found, boundaries at-target). Rules from spec:
  - Runs of equal identity over the evidence; boundary between adjacent runs = resolved P when the prediction predicate holds, else the gap midpoint; confidence `"resolved"` iff P or gap ≤ that pair's target.
  - First segment starts at 0.0 (`"resolved"` by convention); last segment runs to `duration`.
  - Drop a `None` run whose span < `phantom_min`; drop an identified single-probe run whose span < `phantom_min` **and** whose own probe's confidence < `singleton_confidence_keep` (the *sample's* confidence, per the #7 lesson in identify.py).
  - After drops, merge adjacent equal-identity runs (no new segment); a boundary across a dropped region sits at the dropped span's center, `"coarse"`.
  - No evidence at all → one unidentified `"coarse"` segment covering the file.

- [ ] **Step 1: Write the failing tests**

```python
"""segments(): folding evidence into a tracklist, with phantom handling."""

from setlist_maker.boundary import BoundaryEngine, EngineConfig, Probe


def probe(t, artist=None, title=None, window=30.0, purpose="coverage",
          confidence=0.9, offsets=None):
    result = None
    if title is not None:
        result = {"artist": artist or "X", "title": title, "confidence": confidence}
    return Probe(t=t, window=window, purpose=purpose, result=result, offsets=offsets)


def test_no_evidence_yields_one_unidentified_segment():
    segs, drops = BoundaryEngine(600.0).segments()
    assert len(segs) == 1 and segs[0].info is None and segs[0].start == 0.0
    assert drops == []


def test_two_tracks_boundary_at_gap_midpoint_when_unpredicted():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))    # mid 100
    eng.add_probe(probe(285.0, title="B"))   # mid 300
    segs, _ = eng.segments()
    assert [s.info["title"] if s.info else None for s in segs] == ["A", "B"]
    assert segs[0].start == 0.0 and segs[0].confidence == "resolved"
    assert segs[1].start == 200.0 and segs[1].confidence == "coarse"  # gap 200 > 5


def test_resolved_prediction_places_boundary_at_p():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(60.0, title="A"))
    eng.add_probe(probe(300.0, title="B", offsets=[{"offset": 150.0, "timeskew": 0.0}]))
    eng.add_probe(probe(390.0, title="B", offsets=[{"offset": 240.0, "timeskew": 0.0}]))
    eng.add_probe(probe(152.0, title="B", window=12.0, purpose="refine",
                        offsets=[{"offset": 2.0, "timeskew": 0.0}]))
    segs, _ = eng.segments()
    b = next(s for s in segs if s.info and s.info["title"] == "B")
    assert abs(b.start - 150.0) < 1.0 and b.confidence == "resolved"


def test_phantom_single_probe_low_confidence_is_dropped_and_merged():
    # A phantom's span is measured boundary-to-boundary, so it must be PINNED
    # by contradicting evidence on BOTH sides before it reads as small -- a
    # lone C with a wide-open flank might genuinely span that flank, and the
    # rule deliberately refuses to drop it until refinement squeezes it.
    cfg = EngineConfig(phantom_min=20.0, singleton_confidence_keep=0.6)
    eng = BoundaryEngine(600.0, cfg)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(224.0, title="A", window=12.0, purpose="refine"))
    eng.add_probe(probe(240.0, title="C", window=12.0, purpose="refine",
                        confidence=0.2))
    eng.add_probe(probe(250.0, title="A", window=12.0, purpose="refine"))
    # C's span: (230+246)/2 .. (246+256)/2 = 238..251 -> 13s < phantom_min.
    segs, drops = eng.segments()
    assert [s.info["title"] for s in segs if s.info] == ["A"]
    assert len(segs) == 1  # merged straight through the dropped phantom
    assert any(d["type"] == "phantom_dropped" for d in drops)


def test_unpinned_single_probe_track_is_not_dropped():
    # Same C, but its left flank is 146s of open water: it may really span it.
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(240.0, title="C", window=12.0, purpose="refine",
                        confidence=0.2))
    eng.add_probe(probe(250.0, title="A", window=12.0, purpose="refine"))
    segs, drops = eng.segments()
    assert "C" in [s.info["title"] for s in segs if s.info]
    assert drops == []


def test_confident_single_probe_track_survives():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(224.0, title="A", window=12.0, purpose="refine"))
    eng.add_probe(probe(240.0, title="C", window=12.0, purpose="refine",
                        confidence=0.9))
    eng.add_probe(probe(250.0, title="A", window=12.0, purpose="refine"))
    segs, drops = eng.segments()
    assert "C" in [s.info["title"] for s in segs if s.info]
    assert drops == []


def test_short_none_blip_is_absorbed():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(224.0, title="A", window=12.0, purpose="refine"))
    eng.add_probe(probe(240.0, window=12.0, purpose="refine"))   # None blip
    eng.add_probe(probe(250.0, title="A", window=12.0, purpose="refine"))
    segs, drops = eng.segments()
    assert len(segs) == 1 and segs[0].info["title"] == "A"


def test_long_none_region_becomes_unidentified_segment():
    eng = BoundaryEngine(900.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(385.0))
    eng.add_probe(probe(485.0))
    eng.add_probe(probe(785.0, title="B"))
    segs, _ = eng.segments()
    assert [s.info["title"] if s.info else None for s in segs] == ["A", None, "B"]


def test_boundary_stats_counts_pairs_and_resolution():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(285.0, title="B"))     # coarse boundary (gap 200)
    eng.add_probe(probe(430.0, title="C"))
    eng.add_probe(probe(433.0, title="C", window=12.0))
    found, at_target = eng.boundary_stats()
    assert found == 2 and at_target == 0
```

- [ ] **Step 2: Run** — `pytest tests/test_boundary_segments.py -v` — Expected: FAIL with `AttributeError: ... no attribute 'segments'`

- [ ] **Step 3: Implement** (add to `BoundaryEngine`)

```python
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
                    {"type": "phantom_dropped", "kind": "gap",
                     "start": round(starts[i], 1), "extent": round(span, 1)}
                )
                continue
            if ident is not None and len(run) == 1 and span < cfg.phantom_min:
                conf = (run[0].probe.result or {}).get("confidence") or 0
                if conf < cfg.singleton_confidence_keep:
                    meta = self._cluster_meta.get(ident) or {}
                    drops.append(
                        {"type": "phantom_dropped", "kind": "track",
                         "artist": meta.get("artist"), "title": meta.get("title"),
                         "start": round(starts[i], 1), "extent": round(span, 1)}
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
            if (
                self._resolved_by_prediction(left, right) is not None
                or right.mid - left.mid <= self._target(left, right)
            ):
                at_target += 1
        return found, at_target
```

- [ ] **Step 4: Run tests** — all PASS; full suite green; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/boundary.py tests/test_boundary_segments.py
git commit -m "feat(boundary): segments fold with phantom filtering and boundary stats"
```

---

### Task 8: boundary.py — event emission

**Files:**
- Modify: `setlist_maker/boundary.py`
- Test: `tests/test_boundary_engine.py` (append)

**Interfaces:**
- Consumes: Tasks 4–7.
- Produces: `add_probe()` now returns a list of event dicts, each with a `"type"` key: always one `probe_result`; plus, when applicable, `track_discovered`, `cut_in_detected`, `interval_split`, `boundary_predicted`, `boundary_confirmed`, `interval_retired`. (`phantom_dropped` comes from `segments()`, Task 7; `budget_exhausted` / `finalized` are driver-emitted, Task 12.) Event payloads carry human-readable fields (`artist`, `title`, times rounded to 0.1s) — the phase-2 visualizer's raw material.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_boundary_engine.py`)

```python
def _types(events):
    return [e["type"] for e in events]


def test_add_probe_emits_probe_result_and_discovery():
    eng = BoundaryEngine(600.0)
    evs = eng.add_probe(probe(85.0, title="A"))
    assert "probe_result" in _types(evs) and "track_discovered" in _types(evs)
    evs = eng.add_probe(probe(185.0, title="A"))
    assert "track_discovered" not in _types(evs)  # known cluster


def test_interval_split_and_retire_events():
    eng = BoundaryEngine(170.0)
    evs = eng.add_probe(probe(75.0, title="A"))  # mid 90
    assert "interval_split" in _types(evs)
    # Both children (0..90, 90..170) are within the 90s stride -> born retired.
    assert "interval_retired" in _types(evs)


def test_boundary_predicted_fires_when_trust_established():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    evs = eng.add_probe(probe(300.0, title="B", offsets=[off(150.0)]))
    assert "boundary_predicted" not in _types(evs)  # one probe, no corroboration
    evs = eng.add_probe(probe(390.0, title="B", offsets=[off(240.0)]))
    predicted = [e for e in evs if e["type"] == "boundary_predicted"]
    assert predicted and abs(predicted[0]["predicted_start"] - 150.0) < 1.0


def test_boundary_confirmed_fires_when_verification_lands():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    eng.add_probe(probe(300.0, title="B", offsets=[off(150.0)]))
    eng.add_probe(probe(390.0, title="B", offsets=[off(240.0)]))
    evs = eng.add_probe(probe(152.0, title="B", window=12.0, purpose="refine",
                              offsets=[off(2.0)]))
    confirmed = [e for e in evs if e["type"] == "boundary_confirmed"]
    assert confirmed and abs(confirmed[0]["start"] - 150.0) < 1.0


def test_cut_in_detected_when_verify_answers_previous_track():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    # B cut in mid-song: offsets imply start 150 but B really began ~250.
    eng.add_probe(probe(300.0, title="B", offsets=[off(150.0)]))
    eng.add_probe(probe(390.0, title="B", offsets=[off(240.0)]))
    evs = eng.add_probe(probe(152.0, title="A", window=12.0, purpose="refine"))
    assert "cut_in_detected" in _types(evs)
```

- [ ] **Step 2: Run** — Expected: FAIL (`add_probe` currently returns `[]`).

- [ ] **Step 3: Implement** — replace `add_probe` with:

```python
    def add_probe(self, probe: Probe) -> list[dict]:
        """Fold one completed probe into the model and report what changed.

        Events are computed by snapshotting interval statuses and per-cluster
        predictions before/after the insert -- no incremental bookkeeping to
        drift out of sync with the fold."""
        before = {
            (round(l.mid, 3), round(r.mid, 3)): self._status(l, r)
            for l, r in self._pairs()
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
                {"type": "track_discovered", "artist": meta.get("artist"),
                 "title": meta.get("title"), "at": round(probe.mid, 1)}
            )
        if (
            enclosing is not None
            and self._is_boundary(*enclosing)
            and identity == enclosing[0].identity
        ):
            p_start = self._trusted_start(enclosing[1].identity)
            if p_start is not None and probe.t >= p_start - 0.5:
                events.append(
                    {"type": "cut_in_detected", "at": round(probe.mid, 1),
                     "predicted": round(p_start, 1)}
                )

        after = {
            (round(l.mid, 3), round(r.mid, 3)): self._status(l, r)
            for l, r in self._pairs()
        }
        if after.keys() - before.keys():
            events.append({"type": "interval_split", "at": round(probe.mid, 1)})
        for key, status in after.items():
            prev = before.get(key)
            if status == "retired" and prev != "retired":
                events.append(
                    {"type": "interval_retired",
                     "left": round(key[0], 1), "right": round(key[1], 1)}
                )
            elif status == "resolved" and prev != "resolved":
                l, r = key
                pair = next(
                    (pl, pr) for pl, pr in self._pairs()
                    if round(pl.mid, 3) == l and round(pr.mid, 3) == r
                )
                p_start = self._resolved_by_prediction(*pair)
                rmeta = self._cluster_meta.get(pair[1].identity) or {}
                events.append(
                    {"type": "boundary_confirmed", "start": round(p_start, 1),
                     "artist": rmeta.get("artist"), "title": rmeta.get("title")}
                )
        for key in self._clusters:
            now = self._trusted_start(key)
            if now is not None and pred_before.get(key) is None:
                kmeta = self._cluster_meta.get(key) or {}
                events.append(
                    {"type": "boundary_predicted",
                     "predicted_start": round(now, 1),
                     "artist": kmeta.get("artist"), "title": kmeta.get("title")}
                )
        return events
```

- [ ] **Step 4: Run tests** — all boundary tests PASS (earlier tests ignore the return value, so they stay green); full suite green; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/boundary.py tests/test_boundary_engine.py
git commit -m "feat(boundary): emit structured events from the probe fold"
```

---

### Task 9: Fake oracle and property suite (measured ε)

**Files:**
- Create: `tests/boundary_oracle.py` (helper module, not collected as tests)
- Create: `tests/test_boundary_properties.py`

**Interfaces:**
- Consumes: the complete engine (Tasks 4–8).
- Produces: `SyntheticTrack(artist, title, start, cut_in=0.0, confidence=0.9)`, `SyntheticSet(duration, tracks, gaps=(), seed=0, offset_jitter=0.0, offset_dropout=0.0, edge_blur=True)` with `.answer(t, window) -> tuple[dict | None, list[dict] | None]` and `.boundaries() -> list[float]`, and `run_engine(engine, oracle, max_probes=2000) -> int`. Used again by the driver tests (Task 12).

- [ ] **Step 1: Write the oracle helper** (`tests/boundary_oracle.py`)

```python
"""Synthetic recordings and a fake Shazam for engine and driver tests.

The oracle answers a probe the way Shazam would: the dominant track in the
window, an offset that reflects where in the song the window landed
(including cut-ins: a track played from its 1:00 mark reports offsets 60s
larger than its recording position implies), and configurable imperfections.
Deterministic for a given seed, which the fold's replay-equality tests rely on.
"""

from dataclasses import dataclass, field
import random

from setlist_maker.boundary import Probe, ProbePlan


@dataclass(frozen=True)
class SyntheticTrack:
    artist: str
    title: str
    start: float  # position in the recording
    cut_in: float = 0.0  # seconds into the song at which playback began
    confidence: float = 0.9


@dataclass
class SyntheticSet:
    duration: float
    tracks: list[SyntheticTrack]
    gaps: tuple = ()  # [(start, end)] unidentifiable spans
    seed: int = 0
    offset_jitter: float = 0.0  # +- uniform noise on offsets, seconds
    offset_dropout: float = 0.0  # probability a result carries no offsets
    edge_blur: bool = True  # windows straddling a boundary pick a side by share
    rng: random.Random = field(init=False)

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.tracks = sorted(self.tracks, key=lambda tr: tr.start)

    def boundaries(self) -> list[float]:
        return [tr.start for tr in self.tracks[1:]]

    def _in_gap(self, t: float) -> bool:
        return any(a <= t < b for a, b in self.gaps)

    def track_at(self, t: float) -> SyntheticTrack | None:
        if self._in_gap(t) or not self.tracks or t < self.tracks[0].start:
            return None
        current = None
        for tr in self.tracks:
            if tr.start <= t:
                current = tr
        return current

    def answer(self, t: float, window: float) -> tuple[dict | None, list[dict] | None]:
        lo, hi = t, t + window
        # Which track dominates the window? Blurred pick when straddling.
        a, b = self.track_at(lo), self.track_at(hi)
        if a is b:
            track = a
        elif self.edge_blur:
            cut = b.start if b else next(g[0] for g in self.gaps if lo < g[0] <= hi)
            share_a = (cut - lo) / window
            track = a if self.rng.random() < share_a else b
        else:
            track = a if (t + window / 2) < (b.start if b else hi) else b
        if track is None:
            return None, None

        result = {
            "artist": track.artist,
            "title": track.title,
            "confidence": track.confidence,
            "coverart_url": None,
            "shazam_url": None,
            "album": None,
        }
        if self.rng.random() < self.offset_dropout:
            return result, None
        offset = (t - track.start) + track.cut_in
        offset += self.rng.uniform(-self.offset_jitter, self.offset_jitter)
        return result, [{"offset": max(0.0, offset), "timeskew": 0.0}]

    def probe_for(self, plan: ProbePlan) -> Probe:
        result, offsets = self.answer(plan.t, plan.window)
        return Probe(t=plan.t, window=plan.window, purpose=plan.purpose,
                     result=result, offsets=offsets)


def run_engine(engine, oracle: SyntheticSet, max_probes: int = 2000) -> int:
    n = 0
    while (plan := engine.next_probe()) is not None:
        assert n < max_probes, "engine failed to converge"
        engine.add_probe(oracle.probe_for(plan))
        n += 1
    return n
```

- [ ] **Step 2: Write the failing property tests** (`tests/test_boundary_properties.py`)

```python
"""Whole-engine properties: correctness, cost, anytime behavior, measured
precision. If `test_measured_precision_supports_defaults` fails, the DEFAULTS
are wrong, not the test: adjust `EngineConfig.refine_window`/`precision` per
the spec ("the default window/precision pair must be supported by measurement")
and record the change in the spec's Errata."""

import random

from setlist_maker.boundary import BoundaryEngine, EngineConfig, Probe
from tests.boundary_oracle import SyntheticSet, SyntheticTrack, run_engine


def _random_set(seed, duration=14400.0, n_tracks=40, cut_in_rate=0.5):
    rng = random.Random(seed)
    starts = sorted(rng.uniform(120.0, duration - 240.0) for _ in range(n_tracks - 1))
    # Enforce the spec's floor: tracks are >= 2 minutes apart.
    keep, last = [0.0], 0.0
    for s in starts:
        if s - last >= 120.0:
            keep.append(s)
            last = s
    tracks = [
        SyntheticTrack(
            artist=f"Artist {i}", title=f"Track {i}", start=s,
            cut_in=rng.uniform(0.0, 90.0) if rng.random() < cut_in_rate else 0.0,
        )
        for i, s in enumerate(keep)
    ]
    # Blur is opt-in per test: the "clean oracle" cases answer by window
    # midpoint, the measurement gate turns edge_blur back on.
    return SyntheticSet(duration=duration, tracks=tracks, seed=seed, edge_blur=False)


def _boundary_errors(engine, oracle):
    segs, _ = engine.segments()
    starts = [s.start for s in segs]
    return [min(abs(b - s) for s in starts) for b in oracle.boundaries()]


def test_clean_oracle_finds_every_boundary_within_precision():
    oracle = _random_set(seed=1)
    engine = BoundaryEngine(oracle.duration)
    n = run_engine(engine, oracle)
    errors = _boundary_errors(engine, oracle)
    assert max(errors) <= engine.cfg.precision
    # Cost: coverage (~160) + a few refines per boundary.
    assert n <= 160 + 6 * len(oracle.boundaries())


def test_no_track_of_at_least_two_minutes_is_missed():
    oracle = _random_set(seed=2)
    engine = BoundaryEngine(oracle.duration)
    run_engine(engine, oracle)
    segs, _ = engine.segments()
    titles = {s.info["title"] for s in segs if s.info}
    assert titles == {tr.title for tr in oracle.tracks}


def test_anytime_prefix_always_yields_complete_tracklist():
    oracle = _random_set(seed=3, duration=3600.0, n_tracks=12)
    engine = BoundaryEngine(oracle.duration)
    worst = []
    n = 0
    while (plan := engine.next_probe()) is not None and n < 2000:
        engine.add_probe(oracle.probe_for(plan))
        n += 1
        segs, _ = engine.segments()
        assert segs and segs[0].start == 0.0  # complete at every prefix
        errors = _boundary_errors(engine, oracle)
        worst.append(max(errors) if errors else 0.0)
    # Uncertainty must trend down: the final read beats the first by a lot.
    assert worst[-1] <= worst[len(worst) // 4]


def test_replay_equality():
    oracle = _random_set(seed=4, duration=3600.0, n_tracks=10)
    engine = BoundaryEngine(oracle.duration)
    run_engine(engine, oracle)
    replayed = BoundaryEngine(oracle.duration)
    for p in engine.probes:
        replayed.add_probe(
            Probe(p.t, p.window, p.purpose, p.result, p.offsets)
        )
    assert replayed.next_probe() is None
    assert replayed.segments() == engine.segments()


def test_measured_precision_supports_defaults():
    """MEASUREMENT GATE (spec: Probe windows). Noisy conditions: offset jitter,
    30% offset dropout, blurred boundary windows. p90 of boundary error must
    stay within the default precision target."""
    errors = []
    for seed in range(5):
        oracle = _random_set(seed=100 + seed, duration=7200.0, n_tracks=20)
        oracle.offset_jitter = 1.0
        oracle.offset_dropout = 0.3
        oracle.edge_blur = True
        engine = BoundaryEngine(oracle.duration)
        run_engine(engine, oracle)
        errors.extend(_boundary_errors(engine, oracle))
    errors.sort()
    p90 = errors[int(len(errors) * 0.9)]
    assert p90 <= EngineConfig().precision, (
        f"p90 boundary error {p90:.1f}s exceeds the default precision target; "
        "adjust EngineConfig defaults per the spec's measurement gate and "
        "record it in the spec Errata"
    )


def test_gap_heavy_set_converges_with_unidentified_segments():
    tracks = [SyntheticTrack("A", "A", 0.0), SyntheticTrack("B", "B", 1200.0)]
    oracle = SyntheticSet(duration=1800.0, tracks=tracks,
                          gaps=((500.0, 900.0),), seed=5)
    engine = BoundaryEngine(oracle.duration)
    n = run_engine(engine, oracle)
    segs, _ = engine.segments()
    assert any(s.info is None for s in segs)
    assert n < 200
```

- [ ] **Step 3: Run** — `pytest tests/test_boundary_properties.py -v`. These exercise existing code, so failures here are **engine bugs or default-tuning findings, not missing features**. Debug the engine until green (use `superpowers:systematic-debugging` if anything is surprising). If only the measurement gate fails, tune `EngineConfig` (e.g. `refine_window` up, or `precision` to 6–8s), re-run, and note the adjustment for the spec Errata (Task 15).

- [ ] **Step 4: Full suite + ruff green.**

- [ ] **Step 5: Commit**

```bash
git add tests/boundary_oracle.py tests/test_boundary_properties.py
git commit -m "test(boundary): synthetic oracle, property suite, precision measurement gate"
```

---

### Task 10: Progress v2, legacy conversion, sequential guard

**Files:**
- Create: `setlist_maker/adaptive.py`
- Modify: `setlist_maker/identify.py` (sequential resume guard only)
- Test: `tests/test_adaptive_progress.py` (create)

**Interfaces:**
- Consumes: `Probe` (Task 4), `load_progress` from `identify.py`.
- Produces (in `setlist_maker/adaptive.py`): `PROGRESS_VERSION = 2`, `save_progress_v2(duration: float, probes: list[Probe], filepath: Path) -> None`, `load_probes(filepath: Path) -> tuple[list[Probe], float | None]` (handles v2 dicts AND legacy `[timestamp, info]` lists — the legacy format already carries timestamps, only its resume semantics were positional). In `identify.py`: `process_single_file` refuses a v2 progress file with a clear message instead of crashing on a dict.

- [ ] **Step 1: Write the failing tests**

```python
"""Progress v2: fold-ready persistence with legacy sequential conversion."""

import json

from setlist_maker.adaptive import load_probes, save_progress_v2
from setlist_maker.boundary import Probe

INFO = {"artist": "X", "title": "A", "confidence": 0.9}


def test_v2_round_trip(tmp_path):
    path = tmp_path / "p.json"
    probes = [
        Probe(t=100.0, window=30.0, purpose="coverage", result=INFO,
              offsets=[{"offset": 10.0, "timeskew": 0.0}]),
        Probe(t=200.0, window=12.0, purpose="refine", result=None, offsets=None),
    ]
    save_progress_v2(3600.0, probes, path)
    loaded, duration = load_probes(path)
    assert duration == 3600.0
    assert loaded == probes


def test_legacy_list_converts_to_coverage_probes(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps([[0, INFO], [30, None], [60, INFO]]))
    loaded, duration = load_probes(path)
    assert duration is None
    assert [p.t for p in loaded] == [0.0, 30.0, 60.0]
    assert all(p.window == 30.0 and p.purpose == "coverage" for p in loaded)
    assert loaded[1].result is None


def test_missing_file_loads_empty(tmp_path):
    loaded, duration = load_probes(tmp_path / "absent.json")
    assert loaded == [] and duration is None


def test_sequential_refuses_v2_progress(tmp_path, capsys):
    import asyncio

    from setlist_maker.identify import process_single_file

    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"")
    progress = tmp_path / "set_progress.json"
    progress.write_text(json.dumps({"version": 2, "audio_duration": 60.0, "probes": []}))

    import setlist_maker.identify as identify_mod

    class FakeAudio:
        def __len__(self):
            return 60_000

        def __getitem__(self, sl):  # slice_audio runs before the progress check
            return self

    original = identify_mod.load_audio
    identify_mod.load_audio = lambda p, allow_partial=False: FakeAudio()
    try:
        result = asyncio.run(
            process_single_file(audio, None, delay_seconds=0, resume=True)
        )
    finally:
        identify_mod.load_audio = original
    assert result is None
    assert "adaptive" in capsys.readouterr().out
```

- [ ] **Step 2: Run** — Expected: FAIL with `ModuleNotFoundError: No module named 'setlist_maker.adaptive'`

- [ ] **Step 3: Implement** — create `setlist_maker/adaptive.py`:

```python
"""Adaptive identify driver: everything impure around the pure engine.

`boundary.py` never touches Shazam, files, clocks or signals; this module owns
all of that. Persistence is deliberately dumb -- the probe list IS the state,
and the engine is rebuilt by replaying it (see the design spec: "state is a
fold over probes")."""

from __future__ import annotations

import json
from pathlib import Path

from setlist_maker.boundary import Probe
from setlist_maker.identify import load_progress

PROGRESS_VERSION = 2


def save_progress_v2(duration: float, probes: list[Probe], filepath: Path) -> None:
    """Write the probe list; called after every probe, like the sequential path."""
    payload = {
        "version": PROGRESS_VERSION,
        "audio_duration": duration,
        "probes": [
            {"t": p.t, "window": p.window, "purpose": p.purpose,
             "result": p.result, "offsets": p.offsets}
            for p in probes
        ],
    }
    with open(filepath, "w") as f:
        json.dump(payload, f, indent=2)


def load_probes(filepath: Path) -> tuple[list[Probe], float | None]:
    """Load saved probes: v2 dicts, or a legacy sequential list.

    The legacy format is `[[timestamp, info], ...]` -- it already carries
    timestamps (only its *resume* was positional), so a half-finished
    sequential run converts straight into coverage probes and resumes as an
    adaptive run with a dense probed prefix. No migration step."""
    data = load_progress(filepath)
    if isinstance(data, dict) and data.get("version") == PROGRESS_VERSION:
        return (
            [
                Probe(t=float(r["t"]), window=float(r["window"]),
                      purpose=r["purpose"], result=r["result"],
                      offsets=r.get("offsets"))
                for r in data["probes"]
            ],
            data.get("audio_duration"),
        )
    return (
        [
            Probe(t=float(ts), window=30.0, purpose="coverage",
                  result=info, offsets=None)
            for ts, info in (data or [])
        ],
        None,
    )
```

In `identify.py`'s `process_single_file`, immediately after `raw_results = load_progress(progress_path)`:

```python
        if isinstance(raw_results, dict):
            print(
                f"  Error: {progress_path.name} was written by adaptive mode. "
                "Resume without --sequential, or pass --no-resume to discard it."
            )
            return None
```

- [ ] **Step 4: Run tests** — all PASS; full suite green; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/adaptive.py setlist_maker/identify.py tests/test_adaptive_progress.py
git commit -m "feat(adaptive): probe-list progress format with legacy conversion"
```

---

### Task 11: identify.py shared helpers — deduplicate flag and finalize_outputs

**Files:**
- Modify: `setlist_maker/identify.py`
- Test: `tests/test_identify_shared.py` (create)

**Interfaces:**
- Consumes: existing `results_to_tracklist`, `process_single_file` tail (summary + md/json writing).
- Produces: `results_to_tracklist(raw_results, source_filename, corrections_db=None, dedup_config=None, *, deduplicate: bool = True)` — when False, corrections still apply but `deduplicate_tracklist` is skipped (the adaptive engine's segments are already one-entry-per-track; running the singleton filter over them would drop *every* track, since each appears exactly once). And `finalize_outputs(tracklist: Tracklist, output_path: Path, summary: bool) -> None` — the extracted tail of `process_single_file` (summary generation + markdown + JSON sidecar + prints), shared verbatim by both drivers.

- [ ] **Step 1: Write the failing tests**

```python
"""Shared identify helpers used by both the sequential and adaptive drivers."""

import json

from setlist_maker.identify import finalize_outputs, results_to_tracklist

RAW = [
    (0, {"artist": "A", "title": "One", "confidence": 0.9}),
    (180, {"artist": "B", "title": "Two", "confidence": 0.9}),
    (400, {"artist": "A", "title": "One", "confidence": 0.9}),
]


def test_deduplicate_false_keeps_single_sample_tracks():
    tracklist = results_to_tracklist(RAW, "set.mp3", deduplicate=False)
    # Every entry survives: no singleton filter, no smoothing, no collapse.
    assert [t.title for t in tracklist.tracks] == ["One", "Two", "One"]


def test_deduplicate_false_still_applies_corrections():
    class FakeDB:
        def get_correction(self, artist, title):
            return ("A!", "One!") if title == "One" else None

    tracklist = results_to_tracklist(RAW, "set.mp3", FakeDB(), deduplicate=False)
    assert tracklist.tracks[0].artist == "A!"
    assert tracklist.tracks[0].original_title == "One"


def test_finalize_outputs_writes_markdown_and_sidecar(tmp_path):
    tracklist = results_to_tracklist(RAW, "set.mp3", deduplicate=False)
    out = tmp_path / "set_tracklist.md"
    finalize_outputs(tracklist, out, summary=False)
    assert out.exists()
    sidecar = json.loads((tmp_path / "set_tracklist.json").read_text())
    assert isinstance(sidecar, list)  # the bare-list contract (see CLAUDE.md)
```

- [ ] **Step 2: Run** — Expected: FAIL (`unexpected keyword argument 'deduplicate'`).

- [ ] **Step 3: Implement.** In `results_to_tracklist`, change the signature to add the keyword-only `deduplicate: bool = True` and replace the dedup call:

```python
    deduped = deduplicate_tracklist(raw_results, dedup_config) if deduplicate else raw_results
```

Extract `finalize_outputs` from `process_single_file`'s tail (the summary block through the two file writes and prints, currently after "Processing complete"):

```python
def finalize_outputs(tracklist: Tracklist, output_path: Path, summary: bool) -> None:
    """Summary + markdown + JSON sidecar writes, shared by both drivers."""
    if summary:
        print("  Generating playlist summary...")
        summary_lines = [
            f"{t.artist} - {t.title}"
            for t in tracklist.tracks
            if not t.rejected and not t.is_unidentified
        ]
        tracklist.summary = generate_summary(summary_lines)

    # Markdown plus JSON sidecar; the sidecar carries coverart_url for chapters.
    with open(output_path, "w") as f:
        f.write(tracklist.to_markdown())

    json_path = output_path.with_suffix(".json")
    with open(json_path, "w") as f:
        json.dump(tracklist.to_json(), f, indent=2)

    print(f"  Saved: {output_path}")
    print(f"  Found {len(tracklist.tracks)} unique tracks")
```

and have `process_single_file` call `finalize_outputs(tracklist, output_path, summary)` in place of the extracted lines.

- [ ] **Step 4: Run tests** — new tests PASS **and the existing identify tests stay green** (the extraction is behavior-preserving); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/identify.py tests/test_identify_shared.py
git commit -m "refactor(identify): share output finalization; optional dedup bypass"
```

---

### Task 12: The adaptive driver

**Files:**
- Modify: `setlist_maker/adaptive.py`
- Test: `tests/test_adaptive_driver.py` (create)

**Interfaces:**
- Consumes: engine (Tasks 4–8), `extract_window` (Task 2), `identify_sample_with_retry(include_offsets=True)` (Task 1), `save_progress_v2`/`load_probes` (Task 10), `results_to_tracklist(deduplicate=False)` + `finalize_outputs` (Task 11), `live_display` (existing; used disabled until Task 13).
- Produces: `process_single_file_adaptive(audio_path, output_dir, delay_seconds, engine_config=None, resume=True, corrections_db=None, summary=True, allow_partial=False, panel=True, budget_seconds=None) -> tuple[Tracklist, Path] | None`; `EventLog` (JSONL, append mode, context manager, `.write(event)`); `format_probe_line(t, purpose, track_info, *, width=80, color=False) -> str`; `_sigint_flag()` context manager with `.stop` attribute (first Ctrl-C sets it, second raises `KeyboardInterrupt`).

- [ ] **Step 1: Write the failing tests**

```python
"""End-to-end adaptive driver against the synthetic oracle (no network)."""

import asyncio
import json
import signal

import setlist_maker.adaptive as adaptive
from setlist_maker.adaptive import (
    EventLog,
    _sigint_flag,
    format_probe_line,
    process_single_file_adaptive,
)
from tests.boundary_oracle import SyntheticSet, SyntheticTrack


class FakeAudio:
    def __init__(self, seconds):
        self._ms = int(seconds * 1000)

    def __len__(self):
        return self._ms


def _wire(monkeypatch, oracle):
    monkeypatch.setattr(
        adaptive, "load_audio",
        lambda p, allow_partial=False: FakeAudio(oracle.duration),
    )
    monkeypatch.setattr(adaptive, "extract_window", lambda a, t, w: (t, w))

    calls = {"n": 0}

    async def fake_identify(shazam, segment, temp_dir, include_offsets=False,
                            on_backoff=None):
        calls["n"] += 1
        result, offsets = oracle.answer(*segment)
        if result and include_offsets:
            result = {**result, "offsets": offsets or []}
        return result

    monkeypatch.setattr(adaptive, "identify_sample_with_retry", fake_identify)
    return calls


def _oracle():
    return SyntheticSet(
        duration=900.0,
        tracks=[
            SyntheticTrack("A", "Alpha", 0.0),
            SyntheticTrack("B", "Beta", 300.0),
            SyntheticTrack("C", "Gamma", 610.0),
        ],
        seed=1,
    )


def test_driver_end_to_end(tmp_path, monkeypatch):
    oracle = _oracle()
    _wire(monkeypatch, oracle)
    result = asyncio.run(
        process_single_file_adaptive(
            audio_path=tmp_path / "set.mp3", output_dir=None,
            delay_seconds=0, summary=False,
        )
    )
    assert result is not None
    tracklist, output_path = result
    titles = [t.title for t in tracklist.tracks]
    assert titles == ["Alpha", "Beta", "Gamma"]
    for true, got in zip([300.0, 610.0], [t.timestamp for t in tracklist.tracks[1:]]):
        assert abs(true - got) <= 5.0
    assert output_path.exists()
    assert (tmp_path / "set_tracklist.json").exists()
    assert (tmp_path / "set_progress.json").exists()
    events = [
        json.loads(line)
        for line in (tmp_path / "set_events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["type"] == "finalized"
    assert any(e["type"] == "probe_result" for e in events)


def test_driver_resume_is_replay(tmp_path, monkeypatch):
    oracle = _oracle()
    calls = _wire(monkeypatch, oracle)
    asyncio.run(process_single_file_adaptive(
        audio_path=tmp_path / "set.mp3", output_dir=None,
        delay_seconds=0, summary=False,
    ))
    first_run = calls["n"]
    asyncio.run(process_single_file_adaptive(
        audio_path=tmp_path / "set.mp3", output_dir=None,
        delay_seconds=0, summary=False,
    ))
    assert calls["n"] == first_run  # converged replay asks Shazam nothing


def test_budget_zero_stops_immediately_but_finalizes(tmp_path, monkeypatch):
    oracle = _oracle()
    calls = _wire(monkeypatch, oracle)
    result = asyncio.run(process_single_file_adaptive(
        audio_path=tmp_path / "set.mp3", output_dir=None,
        delay_seconds=0, summary=False, budget_seconds=0,
    ))
    assert result is not None
    assert calls["n"] == 0
    events = (tmp_path / "set_events.jsonl").read_text()
    assert "budget_exhausted" in events and "finalized" in events


def test_sigint_flag_sets_then_raises():
    with _sigint_flag() as flag:
        assert flag.stop is False
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert flag.stop is True
        try:
            handler(signal.SIGINT, None)
            raised = False
        except KeyboardInterrupt:
            raised = True
        assert raised


def test_event_log_appends(tmp_path):
    path = tmp_path / "e.jsonl"
    with EventLog(path) as log:
        log.write({"type": "a"})
    with EventLog(path) as log:
        log.write({"type": "b"})
    types = [json.loads(line)["type"] for line in path.read_text().splitlines()]
    assert types == ["a", "b"]


def test_format_probe_line_shapes():
    line = format_probe_line(310.0, "refine",
                             {"artist": "B", "title": "Beta", "confidence": 0.9})
    assert "5:10" in line and "Beta" in line
    miss = format_probe_line(310.0, "coverage", None)
    assert "not identified" in miss
```

- [ ] **Step 2: Run** — Expected: FAIL with ImportError (`EventLog` etc. not defined).

- [ ] **Step 3: Implement** — extend `setlist_maker/adaptive.py` (new imports at top: `asyncio`, `contextmanager` from `contextlib`, `shutil`, `signal`, `sys`, `tempfile`, `time`, `Shazam` from `shazamio`, plus `BoundaryEngine`, `EngineConfig` from `.boundary`; `extract_window`, `format_timestamp`, `load_audio` from `.audio`; `CorrectionsDB`, `Tracklist` from `.editor`; `finalize_outputs`, `results_to_tracklist`, `tracklist_output_path` from `.identify`; `identify_sample_with_retry` from `.shazam_client`; `live_display` from `.progress`):

```python
_ANSI_GREEN = "\033[32m"
_ANSI_DIM = "\033[2m"
_ANSI_RESET = "\033[0m"


def format_probe_line(
    t: float, purpose: str, track_info: dict | None, *, width: int = 80, color: bool = False
) -> str:
    """One compact log line per probe: `»  5:10  ✓  90%  B - Beta`.

    The adaptive sibling of identify.format_progress_line(): no [i/total]
    counter (there is no fixed total), a purpose glyph instead ("·" coverage,
    "»" refine)."""
    glyphs = {"coverage": "·", "refine": "»"} if color else {"coverage": ".", "refine": ">"}
    tag = glyphs.get(purpose, " ")
    time_col = f"{format_timestamp(int(t)):>7}"
    found, miss, ellipsis = ("✓", "·", "…") if color else ("+", "-", "...")

    if track_info is None:
        line = f"  {tag} {time_col}  {miss}  not identified"
        return f"{_ANSI_DIM}{line}{_ANSI_RESET}" if color else line

    conf = track_info.get("confidence")
    conf_str = f"{round(conf * 100):>3d}%" if conf is not None else " -- "
    label = f"{track_info.get('artist', '')} - {track_info.get('title', '')}"
    prefix = f"  {tag} {time_col}  {found}  {conf_str}  "
    avail = max(1, width - len(prefix))
    if len(label) > avail:
        label = label[: max(0, avail - len(ellipsis))].rstrip() + ellipsis
    if color:
        return (
            f"  {tag} {time_col}  {_ANSI_GREEN}{found}{_ANSI_RESET}  "
            f"{_ANSI_DIM}{conf_str}{_ANSI_RESET}  {label}"
        )
    return f"  {tag} {time_col}  {found}  {conf_str}  {label}"


class EventLog:
    """Append-only JSONL beside the progress file; the phase-2 visualizer's
    input. Append mode so a resumed run extends history instead of rewriting
    it; flushed per event so a tail -f (or the future visualizer) sees probes
    as they land."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> "EventLog":
        self._fh = open(self.path, "a")
        return self

    def write(self, event: dict) -> None:
        self._fh.write(json.dumps({"at": round(time.time(), 2), **event}) + "\n")
        self._fh.flush()

    def __exit__(self, *exc) -> None:
        self._fh.close()


class _SigintFlag:
    stop = False


@contextmanager
def _sigint_flag():
    """First Ctrl-C: finish the in-flight probe and finalize. Second: abort
    (per-probe persistence means nothing is lost either way)."""
    flag = _SigintFlag()
    previous = signal.getsignal(signal.SIGINT)

    def handle(signum, frame):
        if flag.stop:
            raise KeyboardInterrupt
        flag.stop = True
        print("\n  Stopping after this probe... (Ctrl-C again to abort)")

    signal.signal(signal.SIGINT, handle)
    try:
        yield flag
    finally:
        signal.signal(signal.SIGINT, previous)


async def process_single_file_adaptive(
    audio_path: Path,
    output_dir: Path | None,
    delay_seconds: int,
    engine_config: EngineConfig | None = None,
    resume: bool = True,
    corrections_db: CorrectionsDB | None = None,
    summary: bool = True,
    allow_partial: bool = False,
    panel: bool = True,
    budget_seconds: float | None = None,
) -> tuple[Tracklist, Path] | None:
    """Adaptive sibling of identify.process_single_file: same inputs and
    outputs, different sampling strategy. Anytime: every stopping rule
    (converged, budget, Ctrl-C) funnels through the same finalization."""
    print(f"\n{'=' * 60}")
    print(f"Processing (adaptive): {audio_path.name}")
    print(f"{'=' * 60}")

    base_name = audio_path.stem
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    output_path = tracklist_output_path(audio_path, output_dir)
    progress_path = output_path.with_name(f"{base_name}_progress.json")
    events_path = output_path.with_name(f"{base_name}_events.jsonl")

    try:
        audio = load_audio(audio_path, allow_partial=allow_partial)
    except Exception as e:
        print(f"  Error: Failed to load audio: {e}")
        return None
    duration = len(audio) / 1000.0

    engine = BoundaryEngine(duration, engine_config)
    probes: list = []
    if resume and progress_path.exists():
        probes, _saved_duration = load_probes(progress_path)
        for p in probes:
            engine.add_probe(p)  # replay; events already logged by the prior run
        if probes:
            print(f"  Resuming with {len(probes)} previous probes")

    shazam = Shazam()
    color = sys.stdout.isatty()
    term_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    started = time.monotonic()
    stop_reason = None

    with tempfile.TemporaryDirectory() as temp_dir, EventLog(events_path) as events, \
            _sigint_flag() as flag, live_display(None, False) as display:
        while True:
            if flag.stop:
                stop_reason = "interrupted"
                break
            if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
                events.write({"type": "budget_exhausted",
                              "after_probes": len(probes)})
                stop_reason = "budget"
                break
            plan = engine.next_probe()
            if plan is None:
                break

            segment = extract_window(audio, plan.t, plan.window)
            info = await identify_sample_with_retry(
                shazam, segment, temp_dir, include_offsets=True
            )
            offsets = info.pop("offsets", None) if info else None
            probe = Probe(t=plan.t, window=plan.window, purpose=plan.purpose,
                          result=info, offsets=offsets)
            for event in engine.add_probe(probe):
                events.write(event)
            probes.append(probe)
            save_progress_v2(duration, probes, progress_path)
            display.log(
                format_probe_line(plan.t, plan.purpose, info,
                                  width=term_width, color=color)
            )

            if engine.next_probe() is not None and not flag.stop:
                await asyncio.sleep(delay_seconds)

        segs, drops = engine.segments()
        for d in drops:
            events.write(d)
        events.write({"type": "finalized", "probes": len(probes),
                      "reason": stop_reason or "converged"})

    print("\n  Processing complete. Generating tracklist...")
    raw = [(int(round(s.start)), s.info) for s in segs]
    tracklist = results_to_tracklist(
        raw, audio_path.name, corrections_db, deduplicate=False
    )
    finalize_outputs(tracklist, output_path, summary)
    return tracklist, output_path
```

(`Probe` is already imported at module top from Task 10.)

- [ ] **Step 4: Run tests** — `pytest tests/test_adaptive_driver.py -v` → all PASS; full suite green; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/adaptive.py tests/test_adaptive_driver.py
git commit -m "feat(adaptive): anytime driver with event log, budget, and SIGINT finalize"
```

---

### Task 13: Adaptive progress panel

**Files:**
- Modify: `setlist_maker/progress.py`
- Modify: `setlist_maker/adaptive.py` (wire the panel in)
- Test: `tests/test_adaptive_panel.py` (create)

**Interfaces:**
- Consumes: `progress.py`'s row helpers (`_row`, `_bar`, `_meter`, `_one_line`, `format_duration`, `_FRAME`, `RAIL`, `GAP`), `BoundaryEngine` metrics (`boundary_stats()`, `max_boundary_width`, `estimated_probes_remaining()`, `segments()`).
- Produces: `AdaptiveRunState` dataclass (fields below) with `begin_probe(plan)`, `record(track_info)`, `update_from_engine(engine)`, `begin_cooldown(seconds)`, `begin_backoff(seconds, attempt)`, `finish()`; `render_adaptive_panel(state, width) -> Group` (pure, fixed 4-row body — same discipline as `render_panel`); `ProgressPanel(state, render=render_panel)` and `live_display(state, enabled, render=render_panel)` gain a render parameter (default preserves sequential behavior byte-for-byte). Driver switches from `live_display(None, False)` to the real panel and passes `announce_backoff` through to `identify_sample_with_retry(on_backoff=...)` exactly as the sequential loop does.

`AdaptiveRunState` fields (mirrors `RunState`'s clock discipline — `clock` injected, `started_at` read from it in `__post_init__`; the phase/deadline mechanics are deliberately a small twin of `RunState`'s rather than a shared base: `RunState` has non-default fields, so a default-bearing dataclass mixin can't slot under it without a `kw_only` refactor of the sequential path this plan doesn't want to risk):

```python
@dataclass
class AdaptiveRunState:
    source_name: str
    audio_seconds: int
    delay_seconds: int
    resumed_from: int = 0
    probes_done: int = 0
    hits: int = 0
    tracks_found: int = 0
    boundaries_found: int = 0
    boundaries_at_target: int = 0
    widest_gap: float = 0.0          # widest active boundary interval, seconds
    est_probes_remaining: int = 0
    current_t: float = 0.0           # position of the probe in flight
    current_purpose: str = "coverage"
    current_result: dict | None = None
    phase: str = "identifying"       # identifying | cooldown | backoff | done
    phase_deadline: float | None = None
    retry: int = 0
    max_retries: int = MAX_RETRIES
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
```

Panel rows (same box, title and `_FRAME` colours as the sequential panel, height fixed):
1. progress bar over `fraction = probes_done / (probes_done + est_probes_remaining)` (1.0 when done) + rail = `position / total` (`format_timestamp(current_t)` / audio length, widened like `render_panel` does).
2. latest identified track + confidence meter (same layout as `_track_row`, reading `current_result`).
3. phase row: `{spinner} probing 1:23:45 (refine)…` / cooldown countdown / backoff countdown-then-spinner (copy `_phase_row`'s branch shapes, substituting the probe wording) + `elapsed` rail.
4. stats: `"{tracks_found} tracks · {boundaries_at_target}/{boundaries_found} boundaries sharp · widest ±{widest_gap/2:.0f}s"` + rail `ETA {format_duration(est_probes_remaining * seconds_per_probe)}` where `seconds_per_probe` mirrors `RunState.seconds_per_sample` (elapsed over probes this run, nominal `delay+3` before settling).

- [ ] **Step 1: Write the failing tests**

```python
"""Adaptive panel: pure rendering, fixed height, clock-driven motion."""

from rich.console import Console

from setlist_maker.progress import AdaptiveRunState, render_adaptive_panel


def _lines(state, width=80):
    console = Console(width=width, force_terminal=True, color_system=None)
    with console.capture() as cap:
        console.print(render_adaptive_panel(state, width))
    return [line for line in cap.get().splitlines() if line.strip()]


def _state(**over):
    defaults = dict(
        source_name="set.mp3", audio_seconds=14400, delay_seconds=15,
        probes_done=42, hits=39, tracks_found=11, boundaries_found=10,
        boundaries_at_target=6, widest_gap=44.0, est_probes_remaining=58,
        current_t=7200.0, current_purpose="refine",
        current_result={"artist": "B", "title": "Beta", "confidence": 0.87},
        clock=lambda: 1000.0, started_at=900.0,
    )
    defaults.update(over)
    return AdaptiveRunState(**defaults)


def test_panel_height_is_fixed_across_states():
    heights = set()
    for over in (
        {},
        {"phase": "cooldown", "phase_deadline": 1010.0},
        {"phase": "backoff", "phase_deadline": 1030.0, "retry": 2},
        {"phase": "done"},
        {"current_result": None, "probes_done": 0, "boundaries_found": 0},
        {"current_result": {"artist": "X", "title": "line\nbreak", "confidence": 0.5}},
    ):
        heights.add(len(_lines(_state(**over))))
    assert len(heights) == 1  # 6 rendered lines: box top + 4 rows + box bottom


def test_panel_shows_boundary_stats_and_position():
    text = "\n".join(_lines(_state()))
    assert "6/10" in text and "11 tracks" in text
    assert "2:00:00 / 4:00:00" in text


def test_cooldown_counts_down_from_injected_clock():
    state = _state(phase="cooldown", phase_deadline=1012.0)
    assert "12s" in "\n".join(_lines(state))


def test_narrow_terminal_never_wraps():
    for line in _lines(_state(), width=46):
        assert len(line) <= 46
```

- [ ] **Step 2: Run** — Expected: FAIL with ImportError (`AdaptiveRunState`).

- [ ] **Step 3: Implement.** In `progress.py`: add `AdaptiveRunState` (fields above, plus `__post_init__`, `begin_probe(plan)` setting `current_t/current_purpose/phase="identifying"/phase_deadline=None/retry=0`, `record(track_info)` setting `current_result` when truthy and bumping `probes_done`/`hits`, `update_from_engine(engine)` filling `tracks_found` (identified segments), `boundaries_found`/`boundaries_at_target` (from `boundary_stats()`), `widest_gap` (`max_boundary_width`), `est_probes_remaining`, and the same `begin_cooldown`/`begin_backoff`/`finish`/`elapsed`/`phase_remaining`/`tick`/`seconds_per_sample`-style derived properties as `RunState` — a deliberate small twin, commented as such). Add `render_adaptive_panel(state, width)` reusing `_row`/`_bar`/`_meter`/`_one_line`/`_position`-style code with the four rows specced above (write an adaptive `_position` variant reading `current_t`; reuse `_FRAME`, adding no new phases). Change `ProgressPanel.__init__(self, state, render=render_panel)` storing `self._render`, and `__rich_console__` yielding `self._render(self.state, console.width)`; change `live_display(state, enabled, render=render_panel)` passing it through. Sequential call sites pass nothing → behavior unchanged.

In `adaptive.py`: build the state before the loop, replace `live_display(None, False)` with `live_display(state, live, render=render_adaptive_panel)` (where `live = color and panel`), call `state.begin_probe(plan)` before each identify, define `announce_backoff(wait_time, attempt)` exactly as `process_single_file` does (calling `state.begin_backoff` + `display.log`) and pass `on_backoff=announce_backoff`; after `add_probe` call `state.record(info)`, `state.update_from_engine(engine)`, then `state.begin_cooldown(delay_seconds)` when another probe is coming; `state.finish()` after the loop.

- [ ] **Step 4: Run tests** — panel tests PASS; driver tests from Task 12 still PASS (they run with `panel=True` but a non-tty stdout, so `live` stays False); full suite green; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/progress.py setlist_maker/adaptive.py tests/test_adaptive_panel.py
git commit -m "feat(progress): adaptive run panel with boundary stats and ETA"
```

---

### Task 14: CLI surface

**Files:**
- Modify: `setlist_maker/cli.py`
- Test: `tests/test_cli_adaptive.py` (create)

**Interfaces:**
- Consumes: `process_single_file_adaptive`, `EngineConfig` (imported in `cli.py` from `setlist_maker.adaptive` / `setlist_maker.boundary`).
- Produces: `parse_budget(text: str) -> float` (seconds; `"2h"`, `"45m"`, `"90s"`, bare number = minutes; `ValueError` on junk); identify flags `--sequential`, `--precision` (float, default 5.0), `--budget DURATION`, `--stride` (float, default 90.0), `--refine-window` (float, default 12.0, dest `refine_window`); routing in `cmd_identify`; `--no-smoothing` note under adaptive; epilog updates.

- [ ] **Step 1: Write the failing tests**

```python
"""CLI: budget parsing and adaptive-vs-sequential routing."""

import argparse

import pytest

import setlist_maker.cli as cli
from setlist_maker.cli import parse_budget


def test_parse_budget_units():
    assert parse_budget("2h") == 7200.0
    assert parse_budget("45m") == 2700.0
    assert parse_budget("90s") == 90.0
    assert parse_budget("30") == 1800.0  # bare number = minutes


def test_parse_budget_rejects_junk():
    for bad in ("", "h", "2x", "-5m"):
        with pytest.raises(ValueError):
            parse_budget(bad)


def _args(tmp_path, **over):
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"\x00")
    defaults = dict(
        path=str(audio), edit=False, web_edit=False, chapters=False, cover=None,
        no_artwork=False, reidentify=False, no_resume=False, allow_partial=False,
        no_learn=True, no_summary=True, no_panel=True, output_dir=None, delay=0,
        title_threshold=0.85, artist_threshold=0.9, singleton_confidence=0.6,
        no_smoothing=False, sequential=False, precision=5.0, budget=None,
        stride=90.0, refine_window=12.0,
    )
    defaults.update(over)
    return argparse.Namespace(**defaults)


def _capture(monkeypatch):
    seen = {}

    async def fake_adaptive(**kwargs):
        seen["adaptive"] = kwargs
        return None

    async def fake_sequential(**kwargs):
        seen["sequential"] = kwargs
        return None

    monkeypatch.setattr(cli, "process_single_file_adaptive", fake_adaptive)
    monkeypatch.setattr(cli, "process_single_file", fake_sequential)
    return seen


def test_default_routes_to_adaptive(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):  # fake returns None -> CLI exits 1
        cli.cmd_identify(_args(tmp_path))
    assert "adaptive" in seen and "sequential" not in seen
    cfg = seen["adaptive"]["engine_config"]
    assert cfg.precision == 5.0 and cfg.stride == 90.0


def test_sequential_flag_routes_to_old_path(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, sequential=True))
    assert "sequential" in seen and "adaptive" not in seen


def test_budget_is_parsed_and_forwarded(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, budget="2h"))
    assert seen["adaptive"]["budget_seconds"] == 7200.0


def test_no_smoothing_warns_under_adaptive(tmp_path, monkeypatch, capsys):
    _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, no_smoothing=True))
    assert "--sequential" in capsys.readouterr().out


def test_bad_precision_fails_fast(tmp_path, monkeypatch):
    _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, precision=0.0))
```

- [ ] **Step 2: Run** — Expected: FAIL with ImportError (`parse_budget`).

- [ ] **Step 3: Implement** in `cli.py`:

```python
_BUDGET_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([hms]?)\s*$", re.IGNORECASE)


def parse_budget(text: str) -> float:
    """'2h' / '45m' / '90s' / bare minutes -> seconds. Raises ValueError."""
    match = _BUDGET_RE.match(text or "")
    if not match:
        raise ValueError(f"invalid duration: {text!r} (try 45m, 2h or 90s)")
    value, unit = float(match.group(1)), match.group(2).lower()
    return value * {"h": 3600.0, "m": 60.0, "s": 1.0}.get(unit or "m")
```

Add imports (`re`; `process_single_file_adaptive` from `setlist_maker.adaptive`; `EngineConfig` from `setlist_maker.boundary`). Add the five arguments to `identify_parser` (an "adaptive sampling" argument group; every help string states its default). In `cmd_identify`, extend the fail-fast validation (`--precision`, `--stride`, `--refine-window` must be > 0; `--stride` must exceed `--refine-window`; `--budget` parsed inside try/except ValueError → print + `sys.exit(1)`), then replace the single `asyncio.run(process_single_file(...))` call with routing:

```python
        if args.sequential:
            result = asyncio.run(
                process_single_file(
                    audio_path=audio_path, output_dir=output_dir,
                    delay_seconds=args.delay, resume=not args.no_resume,
                    corrections_db=corrections_db, dedup_config=dedup_config,
                    summary=not args.no_summary, allow_partial=args.allow_partial,
                    panel=not args.no_panel,
                )
            )
        else:
            if args.no_smoothing:
                print(
                    "  Note: --no-smoothing applies to --sequential only; "
                    "adaptive mode adjudicates outliers by re-probing."
                )
            engine_config = EngineConfig(
                stride=args.stride, precision=args.precision,
                refine_window=args.refine_window,
                singleton_confidence_keep=args.singleton_confidence,
                title_threshold=args.title_threshold,
                artist_threshold=args.artist_threshold,
            )
            result = asyncio.run(
                process_single_file_adaptive(
                    audio_path=audio_path, output_dir=output_dir,
                    delay_seconds=args.delay, engine_config=engine_config,
                    resume=not args.no_resume, corrections_db=corrections_db,
                    summary=not args.no_summary, allow_partial=args.allow_partial,
                    panel=not args.no_panel, budget_seconds=budget_seconds,
                )
            )
```

(`budget_seconds` computed in the validation block, `None` when `--budget` absent.) Update the main epilog: the opening description line ("samples a long recording every 30 seconds" → adaptive boundary hunting, `--sequential` for the old scan) and add the new flags under "identify options" with their defaults, keeping the aligned layout.

- [ ] **Step 4: Run tests** — CLI tests PASS; full suite green; ruff clean. Also smoke the help by hand: `setlist-maker identify -h` renders the new group.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/cli.py tests/test_cli_adaptive.py
git commit -m "feat(cli): adaptive by default with --sequential, --precision, --budget, --stride"
```

---

### Task 15: Docs, errata, final verification

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-08-27-adaptive-boundary-detection-design.md` (Errata)

- [ ] **Step 1: Update `CLAUDE.md`.** Add two module sections after the `identify.py` one, written in the file's house style (mechanism + why, cross-referencing the spec):
  - `### setlist_maker/boundary.py` — pure adaptive engine: fold over probes, interval model with per-kind targets (`stride`/`precision`/`precision_none`), priority = width/target, offset prediction as a lower bound with after-P verification, grid-snapped coverage, per-coverage-gap thrash cap, phantom rule replacing A-B-A smoothing, `segments()` callable at any prefix (the anytime property). Note replay-determinism as the resume contract.
  - `### setlist_maker/adaptive.py` — the impure driver: progress v2 (probe list; legacy sequential lists convert on load and resume as a dense prefix), `EventLog` JSONL for the phase-2 visualizer, SIGINT finalize, budget, panel wiring.
  - Amend the `identify.py` section: adaptive is the default; `--sequential` keeps the old scan; sequential refuses v2 progress files; `results_to_tracklist(deduplicate=False)` exists because segments are already one-per-track and the singleton filter would drop everything; `finalize_outputs` shared by both drivers.
  - Amend the `progress.py` section: `AdaptiveRunState`/`render_adaptive_panel`, same fixed-height discipline.

- [ ] **Step 2: Update the spec's Errata** with: the per-coverage-gap cap (`max_refines_per_gap = 12`) replacing the per-boundary cap and why; grid-snapped coverage probes; any `EngineConfig` default adjustments forced by the Task 9 measurement gate; Task 3 spike findings (or its deferral note).

- [ ] **Step 3: Full verification** — `pytest` (entire suite), `ruff check .`, `ruff format --check .`. Fix anything found.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-08-27-adaptive-boundary-detection-design.md
git commit -m "docs: adaptive boundary detection architecture notes and spec errata"
```

---

## Plan Self-Review Notes

- **Spec coverage:** decisions/goals → Tasks 6, 9, 14; interval model/targets → 4, 6; probe windows + measurement → 9; offset prediction/verification (after-P, cut-in) → 5, 6, 8, 9; phantom rule → 7; anytime/budget/SIGINT → 12; persistence/legacy → 10; CLI → 14; events + panel → 8, 12, 13; spike → 3; testing strategy → 1–14 (each task) + 9; docs/errata → 15. The phase-2 visualizer is explicitly out of scope.
- **Known judgment calls an executor should not "fix" silently:** the per-gap cap, grid coverage, `min_corroboration=2` pending spike, the deliberate `AdaptiveRunState` twin (no `kw_only` refactor of `RunState`).
- Exact-value assertions in tests (`149.5`, `200.0`, probe-count ceilings) encode the deterministic fold; if one fails, suspect the engine (or a deliberate default change — then update spec Errata), not the test.
