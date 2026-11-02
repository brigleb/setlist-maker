"""Waveform peaks for the web editor's timeline and scrubber (#57).

The page draws the set's waveform from min/max pairs at ``PEAK_RATE`` per
second: 50/s is 0.02s a pair, finer than the timeline's deepest zoom
(0.05s/px), and 1.4MB for a four-hour set.

Never ``load_audio()``: four hours of PCM is ~2.5GB (see ``sampler.py``).
ffmpeg decodes straight to 8kHz mono s16 on a pipe, and ``audioop.minmax``
reduces each bin in C. Decoding an MP3 is the slow part (~9s for four hours,
single-threaded), so the file is cut into ``SEGMENTS`` stretches decoded in
parallel (~3.4s). Each stretch is padded or trimmed to exactly the bins it
should cover, so a stretch that decodes a few samples long or short cannot
shift the ones after it.

Cached in ``$XDG_CACHE_HOME/setlist-maker/peaks`` (else ``~/.cache``), keyed
on the file's resolved path, mtime and size, so an edited file misses once.
Embed rewrites the MP3's tag, so the first peaks request after an Embed
recomputes.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import subprocess
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from setlist_maker.fileio import atomic_write_bytes

with warnings.catch_warnings():
    # Deprecated in 3.11-3.12 (removed in 3.13, where audioop-lts provides it).
    # pydub imports it anyway.
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

PEAK_RATE = 50  # min/max pairs per second
DECODE_RATE = 8000  # Hz, mono
SEGMENTS = 8
FORMAT_VERSION = 1
# "SMPK", version, rate, pair count, duration (seconds).
HEADER = struct.Struct("<4sIIId")
MAGIC = b"SMPK"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "setlist-maker" / "peaks"


def cache_key(path: Path) -> str:
    st = path.stat()
    raw = "\x1f".join(
        [
            str(path.resolve()),
            str(st.st_mtime_ns),
            str(st.st_size),
            str(PEAK_RATE),
            str(FORMAT_VERSION),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _decode(path: Path, start: float | None, length: float | None) -> bytes:
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    if length is not None:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(DECODE_RATE), "-f", "s16le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True)
    return out.stdout


def _bins(pcm: bytes, count: int | None) -> bytearray:
    """Reduce s16 PCM to ``count`` int8 (min, max) pairs; None takes what is there."""
    step = DECODE_RATE // PEAK_RATE * 2  # bytes per bin
    n = len(pcm) // step if count is None else count
    out = bytearray(n * 2)
    for k in range(n):
        frag = pcm[k * step : (k + 1) * step]
        if len(frag) < 2:
            continue  # past the decoded audio: silence
        lo, hi = audioop.minmax(frag[: len(frag) & ~1], 2)
        out[2 * k] = (lo >> 8) & 0xFF
        out[2 * k + 1] = (hi >> 8) & 0xFF
    return out


def compute_peaks(path: Path, duration: float | None, segments: int = SEGMENTS) -> bytes:
    """The encoded peaks file for ``path``: header, then int8 min/max pairs."""
    if not duration or segments <= 1:
        pairs = _bins(_decode(path, None, None), None)
        seconds = duration or len(pairs) / 2 / PEAK_RATE
        return HEADER.pack(MAGIC, FORMAT_VERSION, PEAK_RATE, len(pairs) // 2, seconds) + pairs
    total = math.ceil(duration * PEAK_RATE)
    # Whole seconds per stretch, so each starts on a bin boundary.
    per = math.ceil(duration / segments)
    plan = []
    for i in range(segments):
        first = i * per * PEAK_RATE
        if first >= total:
            break
        plan.append((i * per, min(per * PEAK_RATE, total - first)))
    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        parts = list(pool.map(lambda p: _bins(_decode(path, p[0], p[1] / PEAK_RATE), p[1]), plan))
    pairs = b"".join(parts)
    return HEADER.pack(MAGIC, FORMAT_VERSION, PEAK_RATE, total, duration) + pairs


def peaks_for(path: Path, duration: float | None) -> bytes:
    """Cached ``compute_peaks()``. One computation per file at a time."""
    key = cache_key(path)
    target = cache_dir() / f"{key}.peaks"
    with _lock_for(key):
        try:
            return target.read_bytes()
        except OSError:
            pass
        data = compute_peaks(path, duration)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, data)
        except OSError:
            pass  # an unwritable cache still serves this request
        return data
