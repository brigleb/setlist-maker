#!/usr/bin/env python3
"""
Setlist Maker - DJ Set Tracklist Generator

Identifies tracks in long audio recordings (DJ sets, radio shows, etc.)
by slicing them into 30-second samples and running each through Shazam.
Supports single files, multiple files, or entire directories.

Requirements:
    pip install setlist-maker

You also need ffmpeg installed on your system:
    macOS: brew install ffmpeg
    Ubuntu/Debian: sudo apt install ffmpeg
    Windows: download from ffmpeg.org and add to PATH

Usage:
    # Single file
    setlist-maker recording.mp3

    # Multiple files
    setlist-maker set1.mp3 set2.mp3 set3.mp3

    # Entire folder
    setlist-maker /path/to/dj_sets/

    # With options
    setlist-maker /path/to/sets/ --delay 20 --output-dir ./tracklists/
"""

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from pydub import AudioSegment
from shazamio import Shazam

from setlist_maker import __version__

# Configuration
SAMPLE_DURATION_MS = 30 * 1000  # 30 seconds in milliseconds
DEFAULT_DELAY_SECONDS = 15  # Pause between API calls
MAX_RETRIES = 5
INITIAL_BACKOFF = 30

# Supported audio extensions
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".aiff"}


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


async def identify_sample_with_retry(
    shazam: Shazam, segment: AudioSegment, temp_dir: str, max_retries: int = MAX_RETRIES
) -> dict | None:
    """
    Identify a single audio segment using Shazam with exponential backoff retry.
    Returns track info dict or None if not identified.
    """
    temp_path = os.path.join(temp_dir, "temp_sample.mp3")
    segment.export(temp_path, format="mp3")

    backoff = INITIAL_BACKOFF
    for attempt in range(max_retries):
        try:
            result = await shazam.recognize(temp_path)
            if result and "track" in result:
                track = result["track"]
                return {
                    "title": track.get("title", "Unknown Title"),
                    "artist": track.get("subtitle", "Unknown Artist"),
                    "shazam_url": track.get("url"),
                    "album": track.get("sections", [{}])[0].get("metadata", [{}])[0].get("text")
                    if track.get("sections")
                    else None,
                }
            return None
        except Exception as e:
            error_str = str(e).lower()

            # Check if it's a rate limit error
            if "429" in error_str or "too many" in error_str or "rate" in error_str:
                if attempt < max_retries - 1:
                    # Add jitter to avoid thundering herd
                    jitter = random.uniform(0, backoff * 0.1)
                    wait_time = backoff + jitter
                    print(
                        f"\n  Warning: Rate limited. Backing off for {wait_time:.0f} seconds "
                        f"(attempt {attempt + 1}/{max_retries})..."
                    )
                    await asyncio.sleep(wait_time)
                    backoff *= 2  # Exponential backoff
                else:
                    print(f"\n  Error: Rate limit persisted after {max_retries} attempts")
                    return None
            else:
                # Other error - log and return None
                print(f"\n  Error during recognition: {e}")
                return None

    return None


def deduplicate_tracklist(raw_results: list[tuple[int, dict | None]]) -> list[tuple[int, dict | None]]:
    """
    Collapse consecutive identical matches, keeping the first occurrence.
    Preserves unidentified gaps for context.
    """
    tracklist = []
    last_track_key = None
    pending_unidentified = None

    for timestamp, track_info in raw_results:
        if track_info is None:
            # Track unidentified samples but don't add until we see a change
            if last_track_key is not None and pending_unidentified is None:
                pending_unidentified = timestamp
            continue

        # Create a key for comparison
        track_key = (track_info["title"].lower(), track_info["artist"].lower())

        if track_key != last_track_key:
            # If there was an unidentified gap, add it
            if pending_unidentified is not None:
                tracklist.append((pending_unidentified, None))
                pending_unidentified = None

            tracklist.append((timestamp, track_info))
            last_track_key = track_key

    # Handle trailing unidentified
    if pending_unidentified is not None:
        tracklist.append((pending_unidentified, None))

    return tracklist


def generate_markdown(tracklist: list[tuple[int, dict | None]], source_filename: str) -> str:
    """Generate markdown output from the tracklist."""
    lines = [
        f"# Tracklist: {source_filename}",
        "",
        f"*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
    ]

    for i, (timestamp, track_info) in enumerate(tracklist, 1):
        time_str = format_timestamp(timestamp)
        if track_info is None:
            lines.append(f"{i}. **~{time_str}** — *Unidentified*")
        else:
            artist = track_info["artist"]
            title = track_info["title"]
            lines.append(f"{i}. **~{time_str}** — {artist} - {title}")

    lines.append("")
    return "\n".join(lines)


def save_progress(results: list, filepath: Path):
    """Save intermediate results to JSON in case of interruption."""
    # Convert to serializable format
    serializable = [(ts, info) for ts, info in results]
    with open(filepath, "w") as f:
        json.dump(serializable, f, indent=2)


def load_progress(filepath: Path) -> list:
    """Load previous progress if it exists."""
    if filepath.exists():
        with open(filepath, "r") as f:
            return json.load(f)
    return []


async def process_single_file(
    audio_path: Path, output_dir: Path | None, delay_seconds: int, resume: bool = True
) -> Path | None:
    """
    Process a single audio file and generate its tracklist.
    Returns the output path on success, None on failure.
    """
    print(f"\n{'=' * 60}")
    print(f"Processing: {audio_path.name}")
    print(f"{'=' * 60}")

    # Set up output paths
    base_name = audio_path.stem
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{base_name}_tracklist.md"
        progress_path = output_dir / f"{base_name}_progress.json"
    else:
        output_path = audio_path.parent / f"{base_name}_tracklist.md"
        progress_path = audio_path.parent / f"{base_name}_progress.json"

    # Load audio and create slices
    try:
        audio = load_audio(audio_path)
    except Exception as e:
        print(f"  Error: Failed to load audio: {e}")
        return None

    slices = slice_audio(audio, SAMPLE_DURATION_MS)

    # Check for existing progress
    raw_results = []
    start_index = 0
    if resume and progress_path.exists():
        raw_results = load_progress(progress_path)
        start_index = len(raw_results)
        if start_index > 0:
            print(f"  Resuming from sample {start_index + 1} (found {start_index} previous results)")

    # Initialize Shazam
    shazam = Shazam()

    # Process each slice
    total_slices = len(slices)
    with tempfile.TemporaryDirectory() as temp_dir:
        for i, (timestamp, segment) in enumerate(slices[start_index:], start_index + 1):
            time_str = format_timestamp(timestamp)
            print(f"\n  [{i}/{total_slices}] Sample at {time_str}...", end=" ", flush=True)

            track_info = await identify_sample_with_retry(shazam, segment, temp_dir)

            if track_info:
                print(f"Found: {track_info['artist']} - {track_info['title']}")
            else:
                print("Not identified")

            raw_results.append((timestamp, track_info))

            # Save progress after each sample
            save_progress(raw_results, progress_path)

            # Delay before next request (except for the last one)
            if i < total_slices:
                await asyncio.sleep(delay_seconds)

    # Deduplicate and generate output
    print(f"\n  Processing complete. Generating tracklist...")
    tracklist = deduplicate_tracklist(raw_results)
    markdown = generate_markdown(tracklist, audio_path.name)

    # Write output
    with open(output_path, "w") as f:
        f.write(markdown)

    print(f"  Saved: {output_path}")
    print(f"  Found {len(tracklist)} unique tracks")

    # Clean up progress file
    if progress_path.exists():
        os.remove(progress_path)

    return output_path


async def process_batch(
    audio_files: list[Path], output_dir: Path | None, delay_seconds: int, resume: bool = True
):
    """Process multiple audio files in sequence."""

    total_files = len(audio_files)
    print(f"\n{'#' * 60}")
    print(f"# Batch Processing: {total_files} file(s)")
    print(f"# Delay between samples: {delay_seconds} seconds")
    if output_dir:
        print(f"# Output directory: {output_dir}")
    print(f"{'#' * 60}")

    for idx, file in enumerate(audio_files, 1):
        print(f"\n[File {idx}/{total_files}]")
        result = await process_single_file(
            audio_path=file, output_dir=output_dir, delay_seconds=delay_seconds, resume=resume
        )

        if result:
            print(f"\n{'─' * 40}")
            # Print the tracklist
            with open(result, "r") as f:
                print(f.read())
        else:
            print(f"\n  Warning: Failed to process {file.name}")

    print(f"\n{'#' * 60}")
    print(f"# Batch complete! Processed {total_files} file(s)")
    print(f"{'#' * 60}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate tracklists from DJ sets or long audio recordings using Shazam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s recording.mp3                          # Process single file
  %(prog)s set1.mp3 set2.mp3 set3.mp3            # Process multiple files
  %(prog)s /path/to/dj_sets/                     # Process all audio in folder
  %(prog)s ./sets/ -o ./tracklists/ -d 20        # Custom output dir and delay
""",
    )

    parser.add_argument("paths", nargs="+", help="Audio file(s) or directory containing audio files")

    parser.add_argument(
        "-o", "--output-dir", help="Output directory for tracklist files (default: same as input)"
    )

    parser.add_argument(
        "-d",
        "--delay",
        type=int,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay in seconds between API calls (default: {DEFAULT_DELAY_SECONDS})",
    )

    parser.add_argument(
        "--no-resume", action="store_true", help="Start fresh instead of resuming from previous progress"
    )

    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    args = parser.parse_args()

    # Gather all audio files
    audio_files = get_audio_files(args.paths)
    if not audio_files:
        print("Error: No audio files found to process.")
        print(f"Supported formats: {', '.join(sorted(AUDIO_EXTENSIONS))}")
        sys.exit(1)

    print(f"Found {len(audio_files)} audio file(s) to process:")
    for f in audio_files:
        print(f"  - {f.name}")

    # Set up output directory
    output_dir = Path(args.output_dir) if args.output_dir else None

    # Run the batch processor
    asyncio.run(
        process_batch(
            audio_files=audio_files,
            output_dir=output_dir,
            delay_seconds=args.delay,
            resume=not args.no_resume,
        )
    )


if __name__ == "__main__":
    main()
