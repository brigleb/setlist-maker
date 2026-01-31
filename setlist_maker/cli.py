#!/usr/bin/env python3
"""
Setlist Maker - DJ Set Tracklist Generator

Identifies tracks in long audio recordings (DJ sets, radio shows, etc.)
by slicing them into 30-second samples and running each through Shazam.
Supports single files, multiple files, or entire directories.

Features:
    - Automatic track identification via Shazam
    - Interactive TUI editor for reviewing and correcting results
    - Learns from your corrections to improve future identifications
    - Resume interrupted processing sessions

Requirements:
    pip install setlist-maker

You also need ffmpeg installed on your system:
    macOS: brew install ffmpeg
    Ubuntu/Debian: sudo apt install ffmpeg
    Windows: download from ffmpeg.org and add to PATH

Usage:
    # Process audio file and open interactive editor
    setlist-maker recording.mp3 --edit

    # Edit an existing tracklist
    setlist-maker tracklist.md

    # Process without opening editor
    setlist-maker recording.mp3

    # Multiple files
    setlist-maker set1.mp3 set2.mp3 set3.mp3

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
from setlist_maker.editor import (
    CorrectionsDB,
    Track,
    Tracklist,
    parse_markdown_tracklist,
    run_editor,
)

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


def deduplicate_tracklist(
    raw_results: list[tuple[int, dict | None]],
) -> list[tuple[int, dict | None]]:
    """
    Filter and deduplicate track matches.
    1. Remove singletons (tracks appearing only once - likely samples)
    2. Collapse consecutive identical matches
    """
    # Count occurrences of each track
    track_counts: dict[tuple[str, str], int] = {}
    for timestamp, track_info in raw_results:
        if track_info:
            key = (track_info["title"].lower(), track_info["artist"].lower())
            track_counts[key] = track_counts.get(key, 0) + 1

    # Filter: replace singletons with None (treat as unidentified)
    filtered_results = []
    for timestamp, track_info in raw_results:
        if track_info:
            key = (track_info["title"].lower(), track_info["artist"].lower())
            if track_counts[key] == 1:
                filtered_results.append((timestamp, None))  # Singleton = unidentified
            else:
                filtered_results.append((timestamp, track_info))
        else:
            filtered_results.append((timestamp, None))

    # Apply consecutive deduplication
    tracklist = []
    last_track_key = None
    pending_unidentified = None

    for timestamp, track_info in filtered_results:
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


def results_to_tracklist(
    raw_results: list[tuple[int, dict | None]],
    source_filename: str,
    corrections_db: CorrectionsDB | None = None,
) -> Tracklist:
    """
    Convert raw Shazam results to a Tracklist object.
    Applies corrections from the database and deduplicates.
    """
    # Apply corrections before deduplication
    if corrections_db:
        corrected_results = []
        for timestamp, track_info in raw_results:
            if track_info:
                correction = corrections_db.get_correction(
                    track_info["artist"], track_info["title"]
                )
                if correction:
                    track_info = track_info.copy()
                    track_info["original_artist"] = track_info["artist"]
                    track_info["original_title"] = track_info["title"]
                    track_info["artist"], track_info["title"] = correction
            corrected_results.append((timestamp, track_info))
        raw_results = corrected_results

    # Deduplicate
    deduped = deduplicate_tracklist(raw_results)

    # Convert to Track objects
    tracks = []
    for timestamp, track_info in deduped:
        if track_info:
            track = Track(
                timestamp=timestamp,
                artist=track_info.get("artist", ""),
                title=track_info.get("title", ""),
                shazam_url=track_info.get("shazam_url"),
                album=track_info.get("album"),
                original_artist=track_info.get("original_artist"),
                original_title=track_info.get("original_title"),
            )
        else:
            track = Track(timestamp=timestamp, artist="", title="")
        tracks.append(track)

    return Tracklist(
        source_file=source_filename,
        tracks=tracks,
        generated_on=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


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
            lines.append(f"{i}. *Unidentified* ({time_str})")
        else:
            artist = track_info["artist"]
            title = track_info["title"]
            lines.append(f"{i}. **{artist}** - {title} ({time_str})")

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
    audio_path: Path,
    output_dir: Path | None,
    delay_seconds: int,
    resume: bool = True,
    corrections_db: CorrectionsDB | None = None,
) -> tuple[Tracklist, Path] | None:
    """
    Process a single audio file and generate its tracklist.
    Returns (Tracklist, output_path) on success, None on failure.
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
            print(f"  Resuming from sample {start_index + 1} ({start_index} previous results)")

    # Initialize Shazam
    shazam = Shazam()

    # Process each slice
    total_slices = len(slices)
    with tempfile.TemporaryDirectory() as temp_dir:
        for i, (timestamp, segment) in enumerate(slices[start_index:], start_index + 1):
            time_str = format_timestamp(timestamp)
            print(f"  [{i}/{total_slices}] Sample at {time_str}...", end=" ", flush=True)

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

    # Convert to Tracklist with corrections applied
    print("\n  Processing complete. Generating tracklist...")
    tracklist = results_to_tracklist(raw_results, audio_path.name, corrections_db)

    # Generate markdown output
    markdown = tracklist.to_markdown()

    # Write output
    with open(output_path, "w") as f:
        f.write(markdown)

    print(f"  Saved: {output_path}")
    print(f"  Found {len(tracklist.tracks)} unique tracks")

    # Clean up progress file
    if progress_path.exists():
        os.remove(progress_path)

    return tracklist, output_path


async def process_batch(
    audio_files: list[Path],
    output_dir: Path | None,
    delay_seconds: int,
    resume: bool = True,
    open_editor: bool = False,
    use_corrections: bool = True,
) -> list[tuple[Tracklist, Path]]:
    """Process multiple audio files in sequence."""
    corrections_db = CorrectionsDB() if use_corrections else None

    total_files = len(audio_files)
    print(f"\n{'#' * 60}")
    print(f"# Batch Processing: {total_files} file(s)")
    print(f"# Delay between samples: {delay_seconds} seconds")
    if output_dir:
        print(f"# Output directory: {output_dir}")
    if use_corrections:
        print("# Learning mode: enabled (corrections will be remembered)")
    print(f"{'#' * 60}")

    results = []
    for idx, file in enumerate(audio_files, 1):
        print(f"\n[File {idx}/{total_files}]")
        result = await process_single_file(
            audio_path=file,
            output_dir=output_dir,
            delay_seconds=delay_seconds,
            resume=resume,
            corrections_db=corrections_db,
        )

        if result:
            tracklist, output_path = result
            results.append((tracklist, output_path))
            print(f"\n{'─' * 40}")
            # Print the tracklist
            print(tracklist.to_markdown())
        else:
            print(f"\n  Warning: Failed to process {file.name}")

    print(f"\n{'#' * 60}")
    print(f"# Batch complete! Processed {total_files} file(s)")
    print(f"{'#' * 60}")

    # Open editor for the last processed file if requested
    if open_editor and results:
        tracklist, output_path = results[-1]
        print(f"\nOpening interactive editor for: {tracklist.source_file}")
        run_editor(tracklist, output_path, use_corrections=use_corrections)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Generate tracklists from DJ sets or long audio recordings using Shazam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s recording.mp3                          # Process single file
  %(prog)s recording.mp3 --edit                   # Process and open editor
  %(prog)s tracklist.md                           # Edit existing tracklist
  %(prog)s set1.mp3 set2.mp3 set3.mp3            # Process multiple files
  %(prog)s /path/to/dj_sets/                     # Process all audio in folder
  %(prog)s ./sets/ -o ./tracklists/ -d 20        # Custom output dir and delay
""",
    )

    parser.add_argument(
        "paths",
        nargs="+",
        help="Audio file(s), directory, or markdown tracklist to edit",
    )

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
        "-e",
        "--edit",
        action="store_true",
        help="Open interactive editor after processing",
    )

    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start fresh instead of resuming from previous progress",
    )

    parser.add_argument(
        "--no-learn",
        action="store_true",
        help="Disable learning from corrections (don't save/apply corrections)",
    )

    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    args = parser.parse_args()

    # Check if we're editing an existing markdown file
    if len(args.paths) == 1:
        input_path = Path(args.paths[0])
        if input_path.suffix.lower() == ".md" and input_path.is_file():
            # Edit existing tracklist
            print(f"Opening tracklist for editing: {input_path.name}")
            with open(input_path) as f:
                content = f.read()
            tracklist = parse_markdown_tracklist(content)
            if not tracklist.tracks:
                print("Error: Could not parse tracklist from markdown file.")
                sys.exit(1)
            print(f"Loaded {len(tracklist.tracks)} tracks from {tracklist.source_file}")
            run_editor(tracklist, input_path, use_corrections=not args.no_learn)
            return

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
            open_editor=args.edit,
            use_corrections=not args.no_learn,
        )
    )


if __name__ == "__main__":
    main()
