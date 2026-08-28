"""Adaptive identify driver: everything impure around the pure engine.

`boundary.py` never touches Shazam, files, clocks or signals; this module owns
all of that. Persistence is deliberately dumb -- the probe list IS the state,
and the engine is rebuilt by replaying it (see the design spec: "state is a
fold over probes").
"""

from __future__ import annotations

import asyncio
import json
import shutil
import signal
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from shazamio import Shazam

from setlist_maker.audio import extract_window, format_timestamp, load_audio
from setlist_maker.boundary import BoundaryEngine, EngineConfig, Probe
from setlist_maker.editor import CorrectionsDB, Tracklist
from setlist_maker.identify import (
    finalize_outputs,
    load_progress,
    results_to_tracklist,
    tracklist_output_path,
)
from setlist_maker.progress import live_display
from setlist_maker.shazam_client import identify_sample_with_retry

PROGRESS_VERSION = 2


def save_progress_v2(duration: float, probes: list[Probe], filepath: Path) -> None:
    """Write the probe list; called after every probe, like the sequential path."""
    payload = {
        "version": PROGRESS_VERSION,
        "audio_duration": duration,
        "probes": [
            {
                "t": p.t,
                "window": p.window,
                "purpose": p.purpose,
                "result": p.result,
                "offsets": p.offsets,
            }
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
    adaptive run with a dense probed prefix. No migration step.
    """
    data = load_progress(filepath)
    if isinstance(data, dict) and data.get("version") == PROGRESS_VERSION:
        return (
            [
                Probe(
                    t=float(r["t"]),
                    window=float(r["window"]),
                    purpose=r["purpose"],
                    result=r["result"],
                    offsets=r.get("offsets"),
                )
                for r in data["probes"]
            ],
            data.get("audio_duration"),
        )
    return (
        [
            Probe(t=float(ts), window=30.0, purpose="coverage", result=info, offsets=None)
            for ts, info in (data or [])
        ],
        None,
    )


_ANSI_GREEN = "\033[32m"
_ANSI_DIM = "\033[2m"
_ANSI_RESET = "\033[0m"


def format_probe_line(
    t: float, purpose: str, track_info: dict | None, *, width: int = 80, color: bool = False
) -> str:
    """One compact log line per probe: `»  5:10  ✓  90%  B - Beta`.

    The adaptive sibling of identify.format_progress_line(): no [i/total]
    counter (there is no fixed total), a purpose glyph instead ("·" coverage,
    "»" refine).
    """
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
    as they land.
    """

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
    (converged, budget, Ctrl-C) funnels through the same finalization.
    """
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

    with (
        tempfile.TemporaryDirectory() as temp_dir,
        EventLog(events_path) as events,
        _sigint_flag() as flag,
        live_display(None, False) as display,
    ):
        while True:
            if flag.stop:
                stop_reason = "interrupted"
                break
            if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
                events.write({"type": "budget_exhausted", "after_probes": len(probes)})
                stop_reason = "budget"
                break
            plan = engine.next_probe()
            if plan is None:
                break

            segment = extract_window(audio, plan.t, plan.window)
            info = await identify_sample_with_retry(shazam, segment, temp_dir, include_offsets=True)
            offsets = info.pop("offsets", None) if info else None
            probe = Probe(
                t=plan.t,
                window=plan.window,
                purpose=plan.purpose,
                result=info,
                offsets=offsets,
            )
            for event in engine.add_probe(probe):
                events.write(event)
            probes.append(probe)
            save_progress_v2(duration, probes, progress_path)
            display.log(
                format_probe_line(plan.t, plan.purpose, info, width=term_width, color=color)
            )

            if engine.next_probe() is not None and not flag.stop:
                await asyncio.sleep(delay_seconds)

        segs, drops = engine.segments()
        for d in drops:
            events.write(d)
        events.write(
            {"type": "finalized", "probes": len(probes), "reason": stop_reason or "converged"}
        )

    print("\n  Processing complete. Generating tracklist...")
    raw = [(int(round(s.start)), s.info) for s in segs]
    tracklist = results_to_tracklist(raw, audio_path.name, corrections_db, deduplicate=False)
    finalize_outputs(tracklist, output_path, summary)
    return tracklist, output_path
