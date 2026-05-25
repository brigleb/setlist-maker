"""Audio file discovery, loading, and slicing helpers."""

from pathlib import Path

from pydub import AudioSegment

from setlist_maker import AUDIO_EXTENSIONS

# 30-second samples, expressed in milliseconds for pydub slicing.
SAMPLE_DURATION_MS = 30 * 1000


def format_timestamp(seconds: int) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


def get_audio_files(paths: list[str]) -> list[Path]:
    """
    Given a list of paths (files or directories), return all audio files to process.
    """
    audio_files = []

    for path_str in paths:
        path = Path(path_str)
        if path.is_dir():
            # Get all audio files in directory (non-recursive)
            for file in sorted(path.iterdir()):
                if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS:
                    audio_files.append(file)
        elif path.is_file():
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                audio_files.append(path)
            else:
                print(f"Warning: Skipping non-audio file: {path}")
        else:
            print(f"Warning: Path not found: {path}")
    return audio_files


def load_audio(filepath: Path) -> AudioSegment:
    """Load an audio file using pydub."""
    print(f"Loading audio file: {filepath.name}")
    audio = AudioSegment.from_file(str(filepath))
    duration_sec = len(audio) // 1000
    print(f"  Duration: {format_timestamp(duration_sec)} ({duration_sec} seconds)")
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
