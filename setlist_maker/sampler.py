"""One Shazam lookup at a moment the user picked, for the web editor.

The adaptive driver decides where to listen; this lets the user decide instead
("what is playing *here*?"). It deliberately reuses the driver's own pieces --
`identify_sample_with_retry` for the call and the engine's `Probe` for the
answer -- so a hand-picked sample is indistinguishable from one the run took,
apart from its `purpose`, and a later `identify` resume replays it like any
other evidence.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from setlist_maker.boundary import Probe
from setlist_maker.identify import DEFAULT_DELAY_SECONDS

SAMPLE_WINDOW = 30.0


def sample_window_start(t: float, window: float = SAMPLE_WINDOW) -> float:
    """Where to start a window so the audio Shazam fingerprints is centred on ``t``.

    shazamio_core fingerprints a centred 10s excerpt of whatever it is handed,
    so centring the window on the click is what makes the answer describe the
    moment clicked rather than the fifteen seconds after it.
    """
    return max(0.0, t - window / 2.0)


def _slice(audio_path: Path, start: float, window: float):
    """Decode just this window. Never the whole file: four hours of PCM is ~2.5GB."""
    from pydub import AudioSegment

    return AudioSegment.from_file(str(audio_path), start_second=start, duration=window)


async def _recognize(segment) -> dict | None:
    from shazamio import Shazam

    from setlist_maker.shazam_client import identify_sample_with_retry

    with tempfile.TemporaryDirectory() as temp_dir:
        return await identify_sample_with_retry(Shazam(), segment, temp_dir, include_offsets=True)


class ShazamSampler:
    """Serialized, paced Shazam lookups at arbitrary points in one recording.

    The editor's HTTP server is threaded, and the page can ask for several
    samples at once ("sample 3 more"), but Shazam's limit is burst-sensitive
    (see call_log.py), so requests queue on a lock and are spaced
    ``min_interval`` apart -- the same pacing an identify run uses. The first
    request goes straight through.
    """

    def __init__(
        self,
        audio_path: Path,
        min_interval: float = DEFAULT_DELAY_SECONDS,
        recognize: Callable[[object], dict | None] | None = None,
        slicer: Callable[[Path, float, float], object] = _slice,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        last_call: float | None = None,
    ):
        self.audio_path = audio_path
        self.min_interval = min_interval
        self._recognize = recognize or (lambda segment: asyncio.run(_recognize(segment)))
        self._slice = slicer
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: float | None = last_call

    def __call__(self, t: float, window: float = SAMPLE_WINDOW) -> Probe:
        start = sample_window_start(t, window)
        with self._lock:
            if self._last is not None:
                wait = self.min_interval - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            try:
                segment = self._slice(self.audio_path, start, window)
                info = self._recognize(segment)
            finally:
                self._last = self._clock()
        offsets = info.pop("offsets", None) if info else None
        return Probe(t=start, window=window, purpose="manual", result=info, offsets=offsets)


class SampleRequestsClosed(RuntimeError):
    """The run a sample was asked of has finished (or never started)."""


class _Request:
    def __init__(self, t: float):
        self.t = t
        self.done = threading.Event()
        self.probe: Probe | None = None
        self.error: Exception | None = None


class SampleRequests:
    """Moments the user asked about during a live run, served by that run.

    While `identify` is running, the run itself is the only thing that should
    call Shazam -- two callers would double the burst the limit is sensitive
    to, and two writers would race on the progress file. So a click on the
    live timeline is queued here; the adaptive driver takes it ahead of its own
    next probe, inside its normal pacing, and resolves it with the probe it
    recorded. The asking thread (an HTTP handler) blocks until then.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: list[_Request] = []
        self._closed: str | None = None

    def ask(self, t: float, timeout: float | None = None) -> Probe:
        """Queue ``t`` and wait for the run to answer it."""
        request = _Request(t)
        with self._lock:
            if self._closed:
                raise SampleRequestsClosed(self._closed)
            self._queue.append(request)
        if not request.done.wait(timeout):
            with self._lock:
                if request in self._queue:
                    self._queue.remove(request)
            raise TimeoutError(f"no answer for {t:.0f}s within {timeout}s")
        if request.error is not None:
            raise request.error
        return request.probe

    def pop(self) -> _Request | None:
        """The oldest waiting request, for the driver; None when there is none."""
        with self._lock:
            return self._queue.pop(0) if self._queue else None

    def pending(self) -> bool:
        with self._lock:
            return bool(self._queue)

    @staticmethod
    def resolve(request: _Request, probe: Probe) -> None:
        request.probe = probe
        request.done.set()

    @staticmethod
    def abandon(request: _Request, reason: str = "identification stopped") -> None:
        """Fail one request already taken off the queue, so its asker stops waiting."""
        request.error = SampleRequestsClosed(reason)
        request.done.set()

    def close(self, reason: str = "identification has finished") -> None:
        """Refuse new requests and fail the waiting ones: nothing will serve them."""
        with self._lock:
            self._closed = reason
            waiting, self._queue = self._queue, []
        for request in waiting:
            request.error = SampleRequestsClosed(reason)
            request.done.set()
