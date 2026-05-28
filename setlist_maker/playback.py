"""Audio preview playback for the tracklist editor.

Plays a short window of the source recording at a track's start timestamp by
shelling out to ``ffplay`` (which ships with the ffmpeg this project already
requires). Playback runs as a non-blocking subprocess so it never touches the
Textual event loop: the previous in-process (``sounddevice``) implementation
was removed because its audio callbacks locked up the UI (commit 622577d).
"""

import platform
import shutil
import subprocess
import time
from pathlib import Path

# Length of the preview window, mirroring the 30s identification sample
# (audio.SAMPLE_DURATION_MS). ffplay stops at end-of-file on its own, so this
# never needs clamping to the recording's total length.
PREVIEW_SECONDS = 30

PLAYER = "ffplay"


def player_path() -> str | None:
    """Return the path to the ``ffplay`` binary, or None if not installed."""
    return shutil.which(PLAYER)


def audio_output_available() -> bool:
    """True on platforms where we support audio output.

    Playback is supported only on macOS (the tool's target), where a usable
    output device can be assumed. On every other platform we report no output
    so the editor never offers a preview it cannot actually play -- and so the
    capability check stays a cheap, non-blocking call with no subprocess probe.
    """
    return platform.system() == "Darwin"


def playback_available() -> bool:
    """True when a preview can actually be heard from this process."""
    return player_path() is not None and audio_output_available()


class PlaybackController:
    """Owns at most one live ``ffplay`` process for previewing track segments.

    The controller is deliberately dumb -- play / stop / is_playing / elapsed.
    The editor decides *which* segment to play and tracks UI state; this class
    just guarantees there is never more than one preview process alive and that
    it is torn down cleanly.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._started_at: float | None = None

    def play(self, audio_path: Path, start_seconds: int, duration: int = PREVIEW_SECONDS) -> None:
        """Preview ``duration`` seconds from ``start_seconds`` of ``audio_path``.

        Stops any current preview first. Non-blocking: returns as soon as
        ffplay is spawned. ffplay reads ``-ss``/``-t`` off the original file, so
        no temp extraction is needed, and ``-autoexit`` stops it at the window
        end (or end-of-file, whichever comes first).
        """
        self.stop()
        cmd = [
            PLAYER,
            "-ss",
            str(start_seconds),
            "-t",
            str(duration),
            "-nodisp",
            "-autoexit",
            "-hide_banner",
            "-loglevel",
            "error",
            str(audio_path),
        ]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._started_at = time.monotonic()

    def stop(self) -> None:
        """Terminate any live preview. Safe to call when nothing is playing.

        Reaps the child after terminating so it does not linger as a zombie
        (terminate() only signals; wait()/poll() is what clears the process
        table entry and silences Popen's ResourceWarning).
        """
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process = None
        self._started_at = None

    def is_playing(self) -> bool:
        """True while the preview subprocess is still running."""
        return self._process is not None and self._process.poll() is None

    def elapsed(self) -> float:
        """Seconds since the current preview started (0.0 if not playing)."""
        if self._started_at is None or not self.is_playing():
            return 0.0
        return time.monotonic() - self._started_at
