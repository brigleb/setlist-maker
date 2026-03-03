"""
Audio Processor - FFmpeg-based audio processing for DJ set recordings.

Provides functionality for:
    - Joining multiple audio files
    - Smart content-boundary trimming (leading/trailing noise removal)
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
class ContentBoundaries:
    """Detected start/end of actual content (music) in an audio file."""

    content_start: float  # seconds
    content_end: float  # seconds
    total_duration: float  # seconds


@dataclass
class ProcessingConfig:
    """Configuration for audio processing pipeline."""

    # Content trimming
    trim_threshold_db: float = -50.0
    trim_chunk_duration: float = 5.0
    trim_consecutive_chunks: int = 3
    trim_padding_seconds: float = 2.0
    fade_in_duration: float = 3.0
    fade_out_duration: float = 3.0

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
    trim_silence: bool = True
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


def build_filter_chain(
    config: ProcessingConfig,
    boundaries: ContentBoundaries | None = None,
) -> str:
    """
    Build the FFmpeg audio filter chain string.

    Args:
        config: Processing configuration.
        boundaries: Detected content boundaries (for fade calculation).

    Returns:
        Filter chain string for FFmpeg -af parameter.
    """
    filters = []

    # Fade in/out (applied before compression so the ramp is natural)
    if config.trim_silence and boundaries is not None:
        content_duration = boundaries.content_end - boundaries.content_start
        fade_in = min(config.fade_in_duration, content_duration / 2)
        fade_out = min(config.fade_out_duration, content_duration / 2)

        if fade_in > 0:
            filters.append(f"afade=t=in:d={fade_in}")
        if fade_out > 0:
            # fade out starts relative to the trimmed content
            fade_out_start = content_duration - fade_out
            filters.append(f"afade=t=out:st={fade_out_start}:d={fade_out}")

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


def _get_sample_rate(audio_file: Path) -> int:
    """Get the sample rate of an audio file using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_file),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return 44100  # safe default


def detect_content_boundaries(
    audio_file: Path,
    threshold_db: float = -50.0,
    chunk_duration: float = 5.0,
    required_consecutive: int = 3,
    padding_seconds: float = 2.0,
    verbose: bool = False,
) -> ContentBoundaries:
    """
    Detect where actual content (music) starts and ends in an audio file.

    Uses FFmpeg astats to compute RMS levels in fixed-size chunks, then walks
    forward/backward to find runs of consecutive above-threshold chunks.

    Args:
        audio_file: Path to the audio file to analyze.
        threshold_db: RMS threshold in dB — chunks above this are "content".
        chunk_duration: Duration of each analysis chunk in seconds.
        required_consecutive: How many consecutive above-threshold chunks needed.
        padding_seconds: Extra seconds to keep before/after detected boundaries.
        verbose: Print debug info.

    Returns:
        ContentBoundaries with detected start/end times.
    """
    total_duration = get_audio_duration(audio_file)
    if total_duration is None or total_duration <= 0:
        return ContentBoundaries(content_start=0.0, content_end=0.0, total_duration=0.0)

    # For very short files, reduce consecutive requirement
    num_possible_chunks = int(total_duration / chunk_duration)
    effective_consecutive = min(required_consecutive, max(1, num_possible_chunks))

    # Get sample rate to compute astats reset value
    sample_rate = _get_sample_rate(audio_file)
    reset_samples = int(sample_rate * chunk_duration)

    # Run astats with reset to get per-chunk RMS values
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(audio_file),
            "-af",
            f"astats=metadata=1:reset={reset_samples}"
            f",ametadata=print:key=lavfi.astats.Overall.RMS_level",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )

    if result.returncode != 0:
        # Fallback: don't trim
        return ContentBoundaries(
            content_start=0.0, content_end=total_duration, total_duration=total_duration
        )

    # Parse RMS values from stderr
    rms_pattern = re.compile(r"RMS_level=(-?[\d.]+|-inf)")
    rms_values = []
    for m in rms_pattern.finditer(result.stderr):
        val = m.group(1)
        rms_values.append(float("-inf") if val == "-inf" else float(val))

    if not rms_values:
        return ContentBoundaries(
            content_start=0.0, content_end=total_duration, total_duration=total_duration
        )

    # Average into chunk-sized buckets (astats may emit more than one value per chunk)
    expected_chunks = max(1, int(total_duration / chunk_duration))
    values_per_chunk = max(1, len(rms_values) // expected_chunks)
    chunk_rms: list[float] = []
    for i in range(0, len(rms_values), values_per_chunk):
        bucket = [v for v in rms_values[i : i + values_per_chunk] if v != float("-inf")]
        if bucket:
            chunk_rms.append(sum(bucket) / len(bucket))
        else:
            chunk_rms.append(float("-inf"))

    if verbose:
        for i, rms in enumerate(chunk_rms):
            t = i * chunk_duration
            marker = ">>>" if rms > threshold_db else "   "
            print(f"  {marker} {t:7.1f}s  {rms:>7.1f} dB")

    # Walk forward to find first run of N consecutive above-threshold chunks
    content_start_chunk = 0
    run = 0
    for i, rms in enumerate(chunk_rms):
        if rms > threshold_db:
            run += 1
            if run >= effective_consecutive:
                content_start_chunk = i - effective_consecutive + 1
                break
        else:
            run = 0
    else:
        # No content detected — return full duration (safety)
        return ContentBoundaries(
            content_start=0.0, content_end=total_duration, total_duration=total_duration
        )

    # Walk backward to find last run of N consecutive above-threshold chunks
    content_end_chunk = len(chunk_rms) - 1
    run = 0
    for i in range(len(chunk_rms) - 1, -1, -1):
        if chunk_rms[i] > threshold_db:
            run += 1
            if run >= effective_consecutive:
                content_end_chunk = i + effective_consecutive - 1
                break
        else:
            run = 0

    # Convert chunk indices to seconds with padding
    content_start = max(0.0, content_start_chunk * chunk_duration - padding_seconds)
    content_end = min(total_duration, (content_end_chunk + 1) * chunk_duration + padding_seconds)

    if verbose:
        print(f"  Content: {content_start:.1f}s – {content_end:.1f}s (of {total_duration:.1f}s)")

    return ContentBoundaries(
        content_start=content_start,
        content_end=content_end,
        total_duration=total_duration,
    )


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

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Determine the analysis input file
        if len(input_files) > 1:
            # Multiple files: concat to lossless temp WAV first (needed for analysis)
            concat_file = temp_path / "filelist.txt"
            create_concat_file(input_files, concat_file)

            if config.trim_silence:
                # Concat to temp WAV for boundary analysis
                concat_wav = temp_path / "concat.wav"
                concat_cmd = [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c:a",
                    "pcm_s16le",
                    str(concat_wav),
                ]
                if verbose:
                    print(f"Concatenating to temp WAV: {' '.join(concat_cmd)}")
                result = subprocess.run(
                    concat_cmd, capture_output=not verbose, text=True, timeout=3600
                )
                if result.returncode != 0:
                    error_msg = result.stderr if not verbose else "See output above"
                    raise FFmpegError(f"FFmpeg concat failed:\n{error_msg}")
                analysis_file = concat_wav
            else:
                analysis_file = None
        else:
            analysis_file = input_files[0]

        # Detect content boundaries if trimming is enabled
        boundaries = None
        if config.trim_silence and analysis_file is not None:
            boundaries = detect_content_boundaries(
                audio_file=analysis_file,
                threshold_db=config.trim_threshold_db,
                chunk_duration=config.trim_chunk_duration,
                required_consecutive=config.trim_consecutive_chunks,
                padding_seconds=config.trim_padding_seconds,
                verbose=verbose,
            )

        # Build the final encoding command
        cmd = ["ffmpeg", "-y"]

        # Add seeking args if we have boundaries
        if boundaries is not None:
            cmd.extend(["-ss", str(boundaries.content_start)])

        if len(input_files) == 1:
            cmd.extend(["-i", str(input_files[0].absolute())])
        else:
            if config.trim_silence:
                # Use the already-concatenated WAV
                cmd.extend(["-i", str(analysis_file)])
            else:
                concat_file = temp_path / "filelist.txt"
                create_concat_file(input_files, concat_file)
                cmd.extend(["-f", "concat", "-safe", "0", "-i", str(concat_file)])

        if boundaries is not None:
            duration = boundaries.content_end - boundaries.content_start
            cmd.extend(["-t", str(duration)])

        # Add filter chain if any filters are enabled
        filter_chain = build_filter_chain(config, boundaries)
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
