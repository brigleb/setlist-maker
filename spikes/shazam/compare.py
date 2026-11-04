"""M0 spike 1: the same windows through Python's adaptive path and the Rust spike.

For each position, a 30s and a 12s window are each sent once by Python
(`load_audio` + `extract_window` + `identify_sample_with_retry`, the adaptive
driver's own path) and once by `shazam-spike sample`, 15s apart throughout.
Implied start = T + (window - 10) / 2 - offset. Writes JSONL to the path given.

    cargo build --release --manifest-path spikes/shazam/Cargo.toml
    uv run python spikes/shazam/compare.py <audio> <out.jsonl> 1607 4219 ...
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from shazamio import Shazam

from setlist_maker.audio import extract_window, load_audio
from setlist_maker.identify import DEFAULT_DELAY_SECONDS
from setlist_maker.shazam_client import identify_sample_with_retry

HERE = Path(__file__).resolve().parent
SPIKE = HERE / "target" / "release" / "shazam-spike"
DELAY = DEFAULT_DELAY_SECONDS
WINDOWS = (30.0, 12.0)


def implied(start: float, window: float, offsets: list[float]) -> list[float]:
    lead = max(0.0, (window - 10) / 2)
    return [round(start + lead - o, 3) for o in offsets]


async def main() -> None:
    audio_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    positions = [float(a) for a in sys.argv[3:]]
    audio = load_audio(audio_path)
    shazam = Shazam()
    last = 0.0

    async def pace() -> None:
        nonlocal last
        wait = last + DELAY - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        last = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp, out_path.open("a") as out:
        for t in positions:
            for w in WINDOWS:
                await pace()
                info = await identify_sample_with_retry(
                    shazam, extract_window(audio, t, w), tmp, include_offsets=True
                )
                offs = [m["offset"] for m in (info or {}).get("offsets", [])]
                py = {
                    "impl": "python",
                    "start": t,
                    "window": w,
                    "artist": info and info["artist"],
                    "title": info and info["title"],
                    "offsets": offs,
                    "implied": implied(t, w, offs),
                }
                await pace()
                proc = subprocess.run(
                    [str(SPIKE), "sample", str(audio_path), str(t), str(w)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                rs = {"impl": "rust", **json.loads(proc.stdout)}
                for row in (py, rs):
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(
                        f"{row['impl']:>6} {t:>8.1f} w={w:>4.0f}  "
                        f"{row['artist']} - {row['title']}  implied={row['implied']}"
                    )


if __name__ == "__main__":
    asyncio.run(main())
