"""
Audio Processor - FFmpeg-based audio processing for DJ set recordings.

Provides functionality for:
    - Joining multiple audio files
    - Removing leading silence
    - Applying compression for broadcast
    - Normalizing loudness (LUFS)
    - Exporting as MP3 CBR

Requires ffmpeg installed on your system:
    macOS: brew install ffmpeg
    Ubuntu/Debian: sudo apt install ffmpeg
    Windows: download from ffmpeg.org and add to PATH
"""

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcessingConfig:
    """Configuration for audio processing pipeline."""

    # Silence removal
    silence_threshold_db: float = -50.0
    silence_duration: float = 0.1

    # Compression
    compressor_threshold_db: float = -18.0
    compressor_ratio: float = 3.0
    compressor_attack: float = 20.0
    compressor_release: float = 250.0

    # Loudness normalization
    target_loudness: float = -16.0  # LUFS
    true_peak: float = -1.5
    loudness_range: float = 11.0

    # Output
    bitrate: str = "192k"

    # Pipeline toggles
    remove_silence: bool = True
    apply_compression: bool = True
    apply_normalization: bool = True


class FFmpegError(Exception):
    """Raised when FFmpeg operations fail."""

    pass


def check_ffmpeg() -> bool:
    """
    Verify FFmpeg is available on the system.

    Returns:
        True if ffmpeg is available, False otherwise.
    """
    return shutil.which("ffmpeg") is not None


def get_ffmpeg_version() -> str | None:
    """
    Get the FFmpeg version string.

    Returns:
        Version string or None if ffmpeg is not available.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # First line contains version info
            first_line = result.stdout.split("\n")[0]
            return first_line
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def create_concat_file(input_files: list[Path], concat_path: Path) -> None:
    """
    Create an FFmpeg concat demuxer input file.

    Args:
        input_files: List of audio file paths to concatenate.
        concat_path: Path to write the concat file.
    """
    with open(concat_path, "w") as f:
        for audio_file in input_files:
            # Escape single quotes in file paths
            escaped_path = str(audio_file.absolute()).replace("'", "'\\''")
            f.write(f"file '{escaped_path}'\n")


def build_filter_chain(config: ProcessingConfig) -> str:
    """
    Build the FFmpeg audio filter chain string.

    Args:
        config: Processing configuration.

    Returns:
        Filter chain string for FFmpeg -af parameter.
    """
    filters = []

    # Silence removal filter
    if config.remove_silence:
        filters.append(
            f"silenceremove=start_periods=1"
            f":start_threshold={config.silence_threshold_db}dB"
            f":start_duration={config.silence_duration}"
        )

    # Compressor filter
    if config.apply_compression:
        filters.append(
            f"acompressor=threshold={config.compressor_threshold_db}dB"
            f":ratio={config.compressor_ratio}"
            f":attack={config.compressor_attack}"
            f":release={config.compressor_release}"
        )

    # Loudness normalization filter
    if config.apply_normalization:
        filters.append(
            f"loudnorm=I={config.target_loudness}:TP={config.true_peak}:LRA={config.loudness_range}"
        )

    return ",".join(filters)


def process_audio(
    input_files: list[Path],
    output_file: Path,
    config: ProcessingConfig | None = None,
    verbose: bool = False,
) -> Path:
    """
    Process audio files through the full pipeline.

    Pipeline stages:
    1. Concatenate input files (if multiple)
    2. Remove leading silence
    3. Apply compression
    4. Normalize loudness
    5. Export as MP3 CBR

    Args:
        input_files: List of audio files to process (joined in order).
        output_file: Output MP3 file path.
        config: Processing configuration (uses defaults if None).
        verbose: If True, show FFmpeg output.

    Returns:
        Path to the output file.

    Raises:
        FFmpegError: If FFmpeg is not available or processing fails.
        ValueError: If no input files provided.
    """
    if not input_files:
        raise ValueError("No input files provided")

    if not check_ffmpeg():
        raise FFmpegError(
            "FFmpeg not found. Please install it:\n"
            "  macOS: brew install ffmpeg\n"
            "  Ubuntu/Debian: sudo apt install ffmpeg\n"
            "  Windows: download from ffmpeg.org"
        )

    if config is None:
        config = ProcessingConfig()

    # Ensure output directory exists
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Build FFmpeg command
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Build the command
        cmd = ["ffmpeg", "-y"]  # -y to overwrite output

        if len(input_files) == 1:
            # Single file input
            cmd.extend(["-i", str(input_files[0].absolute())])
        else:
            # Multiple files - use concat demuxer
            concat_file = temp_path / "filelist.txt"
            create_concat_file(input_files, concat_file)
            cmd.extend(["-f", "concat", "-safe", "0", "-i", str(concat_file)])

        # Add filter chain if any filters are enabled
        filter_chain = build_filter_chain(config)
        if filter_chain:
            cmd.extend(["-af", filter_chain])

        # Output codec and format
        cmd.extend(
            [
                "-c:a",
                "libmp3lame",
                "-b:a",
                config.bitrate,
                str(output_file.absolute()),
            ]
        )

        # Run FFmpeg
        if verbose:
            print(f"Running: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=not verbose,
            text=True,
            timeout=3600,  # 1 hour timeout for long files
        )

        if result.returncode != 0:
            error_msg = result.stderr if not verbose else "See output above"
            raise FFmpegError(f"FFmpeg processing failed:\n{error_msg}")

    return output_file


def get_audio_duration(audio_file: Path) -> float | None:
    """
    Get the duration of an audio file in seconds using ffprobe.

    Args:
        audio_file: Path to the audio file.

    Returns:
        Duration in seconds, or None if unable to determine.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_file),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None
