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
                info = await identify_sample_with_retry(shazam, seg, temp_dir, include_offsets=True)
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
