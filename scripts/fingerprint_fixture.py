"""Produce the fingerprint fixture with shazamio-core itself (run once).

Writes 12s of deterministic 16 kHz mono PCM as a WAV, asks shazamio-core 1.1.2
for its signature, and saves the data URI. The Rust test regenerates the same
PCM (integer arithmetic only, so both sides agree bit for bit) and must
produce the same URI. 12s rather than 10s, so the centring is tested too.
The test is `signature_is_byte_identical_to_shazamio_cores` in
crates/core/src/shazam/signature.rs.

    uv run python scripts/fingerprint_fixture.py
"""

import asyncio
import tempfile
import wave
from pathlib import Path

from shazamio_core import (
    Recognizer,  # pyright: ignore[reportPrivateImportUsage] -- its stub omits the export
)

OUT = Path(__file__).resolve().parent.parent / "crates/core/src/shazam/lcg_12s.uri"
RATE = 16000
SECONDS = 12
PERIODS = (37, 23, 11, 61, 17)


def test_pcm() -> list[int]:
    """LCG noise plus a square wave whose pitch changes every half second."""
    state = 1
    out = []
    for i in range(RATE * SECONDS):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        noise = ((state >> 16) % 4001) - 2000
        period = PERIODS[(i // 8000) % len(PERIODS)]
        square = 6000 if (i % period) < period // 2 else -6000
        out.append(max(-32768, min(32767, noise + square)))
    return out


async def main() -> None:
    pcm = test_pcm()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fixture.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(s.to_bytes(2, "little", signed=True) for s in pcm))
        sig = await Recognizer().recognize_path(str(path))
    out = OUT
    out.write_text(sig.signature.uri + "\n")
    print(f"{out}: samplems={sig.signature.samples}, {len(sig.signature.uri)} chars")


if __name__ == "__main__":
    asyncio.run(main())
