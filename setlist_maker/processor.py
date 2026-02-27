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

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
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


@dataclass
class AudioAnalysis:
    """Results from analyzing an audio file."""

    duration: float | None = None
    size_bytes: int = 0
    loudness_i: float | None = None  # Integrated loudness (LUFS)
    true_peak: float | None = None  # True peak (dBTP)
    loudness_range: float | None = None  # Loudness range (LU)
    waveform: list[float] = field(default_factory=list)  # Normalized 0.0-1.0 RMS values


def analyze_audio(audio_file: Path, waveform_points: int = 60) -> AudioAnalysis:
    """
    Analyze an audio file for loudness stats and waveform data.

    Runs two read-only FFmpeg passes:
    1. loudnorm filter for loudness statistics
    2. astats filter for per-chunk RMS levels (sparkline data)

    Args:
        audio_file: Path to the audio file.
        waveform_points: Number of waveform data points to return.

    Returns:
        AudioAnalysis with whatever data could be gathered.
    """
    analysis = AudioAnalysis(
        duration=get_audio_duration(audio_file),
        size_bytes=audio_file.stat().st_size if audio_file.exists() else 0,
    )

    # Pass 1: Loudness stats via loudnorm
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(audio_file),
                "-af",
                "loudnorm=print_format=json",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if result.returncode == 0:
            # Extract JSON block from stderr
            match = re.search(r"\{[^}]+\}", result.stderr, re.DOTALL)
            if match:
                stats = json.loads(match.group())
                analysis.loudness_i = float(stats.get("input_i", 0))
                analysis.true_peak = float(stats.get("input_tp", 0))
                analysis.loudness_range = float(stats.get("input_lra", 0))
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, ValueError):
        pass

    # Pass 2: Waveform data via astats + ametadata
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(audio_file),
                "-af",
                "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if result.returncode == 0:
            # Parse RMS dB values from stderr
            rms_pattern = re.compile(r"RMS_level=(-?[\d.]+)")
            rms_values = [
                float(m.group(1))
                for m in rms_pattern.finditer(result.stderr)
                if m.group(1) != "-inf"
            ]

            if rms_values:
                analysis.waveform = _downsample_to_sparkline(rms_values, waveform_points)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass

    return analysis


def _downsample_to_sparkline(db_values: list[float], num_points: int) -> list[float]:
    """
    Downsample RMS dB values to normalized 0.0-1.0 values for sparkline display.

    Splits the input into num_points buckets, averages each bucket,
    then normalizes against the range of values seen.
    """
    if not db_values:
        return []

    # Split into buckets and average
    bucket_size = max(1, len(db_values) // num_points)
    buckets = []
    for i in range(0, len(db_values), bucket_size):
        chunk = db_values[i : i + bucket_size]
        buckets.append(sum(chunk) / len(chunk))

    # Trim to desired number of points
    buckets = buckets[:num_points]

    # Normalize to 0.0-1.0 range
    min_val = min(buckets)
    max_val = max(buckets)
    val_range = max_val - min_val

    if val_range < 0.01:
        # All values roughly the same — flat line at mid-height
        return [0.5] * len(buckets)

    return [(v - min_val) / val_range for v in buckets]


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
