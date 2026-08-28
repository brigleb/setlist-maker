"""Adaptive identify driver: everything impure around the pure engine.

`boundary.py` never touches Shazam, files, clocks or signals; this module owns
all of that. Persistence is deliberately dumb -- the probe list IS the state,
and the engine is rebuilt by replaying it (see the design spec: "state is a
fold over probes").
"""

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
