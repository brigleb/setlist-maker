"""Audio file discovery, loading, and slicing helpers."""

from pathlib import Path

from pydub import AudioSegment
from pydub.utils import mediainfo_json

from setlist_maker import AUDIO_EXTENSIONS

# 30-second samples, expressed in milliseconds for pydub slicing.
SAMPLE_DURATION_MS = 30 * 1000

# Decode-completeness guard. We compare what ffmpeg actually decoded against the
# duration ffprobe reads from the container. A large shortfall means ffmpeg hit
# EOF early -- the classic signature of a file that was still being written or
# downloaded (e.g. iCloud sync) when we read it. The reported duration for a
# partially-materialized iCloud file is the *full* size, so the decode falls
# well short and we can catch it. Tolerances are generous so frame-level padding
# and VBR estimate noise never trip a false alarm.
DECODE_SHORTFALL_REL_TOLERANCE = 0.02  # decode must reach >= 98% of reported
DECODE_SHORTFALL_MIN_GAP_SECONDS = 2.0  # ...and the absolute gap must exceed this


class TruncatedAudioError(Exception):
    """Raised when far less audio decoded than the file claims to contain."""


def format_timestamp(seconds: int) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


def get_audio_file(path_str: str) -> Path | None:
    """
    Validate a single audio file path.

    Returns the Path if it is an existing, supported audio file; otherwise
    prints an error and returns None.
    """
    path = Path(path_str)
    if not path.exists():
        print(f"Error: Path not found: {path}")
        return None
    if not path.is_file():
        print(f"Error: Not a file: {path}")
        return None
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        print(f"Error: Not a supported audio file: {path}")
        print(f"Supported formats: {', '.join(sorted(AUDIO_EXTENSIONS))}")
        return None
    return path


def probe_duration_seconds(filepath: Path) -> float | None:
    """Return the container's reported duration in seconds via ffprobe.

    Reuses the same ffprobe pydub relies on. Best-effort: returns None if ffprobe
    is unavailable, errors, or reports no usable duration, so the caller skips
    the completeness check rather than failing on an exotic input.
    """
    try:
        info = mediainfo_json(str(filepath))
    except Exception:
        return None

    # Prefer the container/format duration; fall back to the first audio stream.
    duration = (info.get("format") or {}).get("duration")
    if duration is None:
        for stream in info.get("streams") or []:
            if stream.get("codec_type") == "audio" and stream.get("duration"):
                duration = stream["duration"]
                break

    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def verify_decode_complete(
    decoded_seconds: float,
    reported_seconds: float | None,
    *,
    rel_tolerance: float = DECODE_SHORTFALL_REL_TOLERANCE,
    min_gap_seconds: float = DECODE_SHORTFALL_MIN_GAP_SECONDS,
) -> None:
    """Raise TruncatedAudioError if the decode fell well short of the file's duration.

    No-ops when there is nothing to compare (no reported duration), when the gap
    is within an absolute floor, or when the decode is within `rel_tolerance` of
    (or longer than) the reported duration.
    """
    if not reported_seconds or reported_seconds <= 0:
        return

    gap = reported_seconds - decoded_seconds
    if gap <= min_gap_seconds:
        return
    if decoded_seconds >= reported_seconds * (1.0 - rel_tolerance):
        return

    raise TruncatedAudioError(
        f"Decoded only {format_timestamp(int(decoded_seconds))} of audio, but the "
        f"file reports {format_timestamp(int(reported_seconds))}. It was likely "
        f"still being written or downloading (e.g. iCloud sync) when it was read. "
        f"Wait for it to finish, then retry -- or pass --allow-partial to process "
        f"the decoded portion anyway."
    )


def load_audio(filepath: Path, *, allow_partial: bool = False) -> AudioSegment:
    """Load an audio file using pydub.

    Unless `allow_partial` is set, guards against a truncated decode (see
    `verify_decode_complete`) so an incompletely written/synced file never
    silently yields a partial tracklist.
    """
    print(f"Loading audio file: {filepath.name}")
    audio = AudioSegment.from_file(str(filepath))
    duration_sec = len(audio) // 1000
    print(f"  Duration: {format_timestamp(duration_sec)} ({duration_sec} seconds)")
    if not allow_partial:
        verify_decode_complete(len(audio) / 1000.0, probe_duration_seconds(filepath))
    return audio


def slice_audio(audio: AudioSegment, sample_duration_ms: int) -> list[tuple[int, AudioSegment]]:
    """
    Slice audio into consecutive chunks.
    Returns list of (start_time_seconds, audio_segment) tuples.
    """
    slices = []
    total_ms = len(audio)
    position = 0

    while position < total_ms:
        end_position = min(position + sample_duration_ms, total_ms)
        segment = audio[position:end_position]
        start_seconds = position // 1000
        slices.append((start_seconds, segment))
        position = end_position

    print(f"  Created {len(slices)} samples of {sample_duration_ms // 1000} seconds each")
    return slices


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
