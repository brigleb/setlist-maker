#!/usr/bin/env python3
"""
Setlist Maker - DJ Set Tracklist Generator

Identifies tracks in long audio recordings (DJ sets, radio shows, etc.)
by slicing them into 30-second samples and running each through Shazam.
Supports single files, multiple files, or entire directories.

Features:
    - Automatic track identification via Shazam
    - Audio processing: join files, remove silence, compress, normalize
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

    # Process and combine audio files
    setlist-maker process part1.wav part2.wav -o "My Set.mp3"

    # Process with custom settings
    setlist-maker process *.wav -o output.mp3 --loudness -14 --bitrate 320k

    # Process and identify tracks
    setlist-maker process *.wav -o output.mp3 --identify --edit
"""

import argparse
import asyncio
import json
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from pydub import AudioSegment
from shazamio import Shazam

from setlist_maker import AUDIO_EXTENSIONS, __version__
from setlist_maker.artwork import create_chapter_image, fetch_artwork
from setlist_maker.chapters import embed_chapters
from setlist_maker.editor import (
    CorrectionsDB,
    Track,
    Tracklist,
    find_audio_file,
    parse_markdown_tracklist,
    run_editor,
)
from setlist_maker.processor import (
    FFmpegError,
    ProcessingConfig,
    check_ffmpeg,
    get_audio_duration,
    process_audio,
)

# Configuration
SAMPLE_DURATION_MS = 30 * 1000  # 30 seconds in milliseconds
DEFAULT_DELAY_SECONDS = 15  # Pause between API calls
MAX_RETRIES = 5
INITIAL_BACKOFF = 30


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
    temp_path = str(Path(temp_dir) / "temp_sample.mp3")
    segment.export(temp_path, format="mp3")

    backoff = INITIAL_BACKOFF
    for attempt in range(max_retries):
        try:
            result = await shazam.recognize(temp_path)
            if result and "track" in result:
                track = result["track"]
                images = track.get("images", {})
                return {
                    "title": track.get("title", "Unknown Title"),
                    "artist": track.get("subtitle", "Unknown Artist"),
                    "shazam_url": track.get("url"),
                    "album": track.get("sections", [{}])[0].get("metadata", [{}])[0].get("text")
                    if track.get("sections")
                    else None,
                    "coverart_url": images.get("coverarthq") or images.get("coverart"),
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
                coverart_url=track_info.get("coverart_url"),
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
            print(f"  [{i}/{total_slices}] Sample at {time_str}")

            track_info = await identify_sample_with_retry(shazam, segment, temp_dir)

            if track_info:
                print(f"  Found: {track_info['artist']} - {track_info['title']}")
            else:
                print("  Not identified")

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
        progress_path.unlink()

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


def format_duration(seconds: float) -> str:
    """Format duration in seconds to human-readable string."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def cmd_process(args: argparse.Namespace) -> None:
    """Handle the 'process' subcommand for audio processing."""
    # Verify FFmpeg is available
    if not check_ffmpeg():
        print("Error: FFmpeg not found. Please install it:")
        print("  macOS: brew install ffmpeg")
        print("  Ubuntu/Debian: sudo apt install ffmpeg")
        print("  Windows: download from ffmpeg.org")
        sys.exit(1)

    # Gather input files
    input_files = []
    for path_str in args.inputs:
        path = Path(path_str)
        if path.is_file():
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                input_files.append(path)
            else:
                print(f"Warning: Skipping non-audio file: {path}")
        else:
            print(f"Warning: File not found: {path}")

    if not input_files:
        print("Error: No valid audio files found.")
        print(f"Supported formats: {', '.join(sorted(AUDIO_EXTENSIONS))}")
        sys.exit(1)

    # Display input files
    print(f"\n{'=' * 60}")
    print("Audio Processing Pipeline")
    print(f"{'=' * 60}")
    print(f"\nInput files ({len(input_files)}):")
    total_duration = 0.0
    for f in input_files:
        duration = get_audio_duration(f)
        if duration:
            total_duration += duration
            print(f"  - {f.name} ({format_duration(duration)})")
        else:
            print(f"  - {f.name}")

    if total_duration > 0:
        print(f"\nTotal input duration: {format_duration(total_duration)}")

    output_path = Path(args.output)
    print(f"\nOutput: {output_path}")

    # Build processing config
    config = ProcessingConfig(
        target_loudness=args.loudness,
        bitrate=args.bitrate,
        remove_silence=not args.no_silence_removal,
        apply_compression=not args.no_compress,
        apply_normalization=not args.no_normalize,
    )

    # Show processing stages
    stages = []
    if len(input_files) > 1:
        stages.append("Concatenate files")
    if config.remove_silence:
        stages.append("Remove leading silence")
    if config.apply_compression:
        stages.append("Apply compression")
    if config.apply_normalization:
        stages.append(f"Normalize loudness ({config.target_loudness} LUFS)")
    stages.append(f"Export MP3 @ {config.bitrate}")

    print("\nProcessing stages:")
    for i, stage in enumerate(stages, 1):
        print(f"  {i}. {stage}")

    # Run processing
    print(f"\n{'─' * 60}")
    print("Processing audio...")

    try:
        result_path = process_audio(
            input_files=input_files,
            output_file=output_path,
            config=config,
            verbose=args.verbose if hasattr(args, "verbose") else False,
        )
        print(f"\n✓ Output saved: {result_path}")

        # Show output file info
        output_duration = get_audio_duration(result_path)
        if output_duration:
            print(f"  Duration: {format_duration(output_duration)}")

        output_size = result_path.stat().st_size
        print(f"  Size: {output_size / (1024 * 1024):.1f} MB")

    except FFmpegError as e:
        print(f"\nError: {e}")
        sys.exit(1)

    # Chain to identification if requested
    if args.identify:
        print(f"\n{'=' * 60}")
        print("Track Identification")
        print(f"{'=' * 60}")

        corrections_db = CorrectionsDB() if not args.no_learn else None

        result = asyncio.run(
            process_single_file(
                audio_path=result_path,
                output_dir=result_path.parent,
                delay_seconds=args.delay,
                resume=True,
                corrections_db=corrections_db,
            )
        )

        if result and args.edit:
            tracklist, tracklist_path = result
            print(f"\nOpening interactive editor for: {tracklist.source_file}")
            run_editor(tracklist, tracklist_path, use_corrections=not args.no_learn)


def cmd_identify(args: argparse.Namespace) -> None:
    """Handle the 'identify' subcommand (default behavior)."""
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


def _load_tracklist_with_artwork_urls(
    tracklist_path: Path,
) -> tuple[Tracklist, dict[int, str]]:
    """
    Load a tracklist and extract any saved cover art URLs.

    Tries the JSON sidecar file first (has coverart_url), falls back to
    parsing the markdown.

    Args:
        tracklist_path: Path to the markdown tracklist file.

    Returns:
        Tuple of (Tracklist, dict mapping track index to coverart_url).
    """
    coverart_urls: dict[int, str] = {}

    # Try loading from JSON sidecar for richer metadata
    json_path = tracklist_path.with_suffix(".json")
    if json_path.exists():
        try:
            with open(json_path) as f:
                json_tracks = json.load(f)

            # Parse markdown for the canonical tracklist structure
            with open(tracklist_path) as f:
                tracklist = parse_markdown_tracklist(f.read())

            # Map coverart URLs from JSON to tracklist tracks by index
            for i, jt in enumerate(json_tracks):
                url = jt.get("coverart_url")
                if url and i < len(tracklist.tracks):
                    tracklist.tracks[i].coverart_url = url
                    coverart_urls[i] = url

            return tracklist, coverart_urls
        except (json.JSONDecodeError, IOError):
            pass

    # Fallback: parse markdown only (no coverart URLs available)
    with open(tracklist_path) as f:
        tracklist = parse_markdown_tracklist(f.read())

    return tracklist, coverart_urls


def cmd_chapters(args: argparse.Namespace) -> None:
    """Handle the 'chapters' subcommand for embedding chapter markers."""
    tracklist_path = Path(args.tracklist)

    if not tracklist_path.exists():
        print(f"Error: Tracklist file not found: {tracklist_path}")
        sys.exit(1)

    # Load tracklist with any saved artwork URLs
    print(f"Loading tracklist: {tracklist_path.name}")
    tracklist, coverart_urls = _load_tracklist_with_artwork_urls(tracklist_path)

    active_tracks = [t for t in tracklist.tracks if not t.rejected and not t.is_unidentified]
    if not active_tracks:
        print("Error: No identified tracks found in tracklist.")
        sys.exit(1)

    print(f"  Found {len(active_tracks)} tracks")

    # Find the audio file
    if args.audio:
        audio_path = Path(args.audio)
    else:
        audio_path = find_audio_file(tracklist_path)

    if not audio_path or not audio_path.exists():
        print("Error: Could not find the audio file.")
        print("  Use --audio to specify the MP3 file path.")
        sys.exit(1)

    if audio_path.suffix.lower() != ".mp3":
        print(f"Error: Chapter markers require an MP3 file, got: {audio_path.suffix}")
        sys.exit(1)

    print(f"  Audio file: {audio_path.name}")

    # Get all non-rejected tracks (including unidentified) for chapter timing
    chapter_tracks = [t for t in tracklist.tracks if not t.rejected]

    # Fetch artwork and generate chapter images
    chapter_images: dict[int, bytes] = {}
    episode_image: bytes | None = None

    if not args.no_artwork:
        print(f"\n{'─' * 60}")
        print("Fetching artwork...")

        for i, track in enumerate(chapter_tracks):
            if track.is_unidentified:
                print(f"  [{i + 1}/{len(chapter_tracks)}] {track.time_str} - Skipping unidentified")
                continue

            label = f"{track.artist} - {track.title}"
            print(f"  [{i + 1}/{len(chapter_tracks)}] {track.time_str} - {label}")

            # Fetch cover art
            artwork_bytes = fetch_artwork(
                artist=track.artist,
                title=track.title,
                coverart_url=track.coverart_url,
            )

            if artwork_bytes:
                print("    Found artwork, generating chapter image...")
            else:
                print("    No artwork found, using text-only image")

            # Create MTV-style overlay image
            chapter_img = create_chapter_image(
                artwork_bytes=artwork_bytes,
                artist=track.artist,
                title=track.title,
            )
            chapter_images[i] = chapter_img

            # Use first track's artwork as episode cover
            if episode_image is None and artwork_bytes:
                episode_image = create_chapter_image(
                    artwork_bytes=artwork_bytes,
                    artist=tracklist.source_file.replace("_tracklist", "").rsplit(".", 1)[0],
                    title="Tracklist",
                )

        print(f"  Generated {len(chapter_images)} chapter image(s)")

    # Embed chapters into MP3
    print(f"\n{'─' * 60}")
    print("Embedding chapter markers...")

    embed_chapters(
        audio_path=audio_path,
        tracks=chapter_tracks,
        chapter_images=chapter_images if not args.no_artwork else None,
        episode_image=episode_image if not args.no_artwork else None,
    )

    print(f"\n  Embedded {len(chapter_tracks)} chapter(s) into {audio_path.name}")
    if chapter_images:
        print(f"  Embedded {len(chapter_images)} chapter image(s)")
    if episode_image:
        print("  Embedded episode cover art")

    print(f"\n{'=' * 60}")
    print("Done! Chapter markers embedded successfully.")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate tracklists from DJ sets or long audio recordings using Shazam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Identify tracks in audio
  %(prog)s recording.mp3                          # Process single file
  %(prog)s recording.mp3 --edit                   # Process and open editor
  %(prog)s tracklist.md                           # Edit existing tracklist

  # Process audio files
  %(prog)s process part1.wav part2.wav -o set.mp3 # Join and process files
  %(prog)s process *.wav -o out.mp3 --identify    # Process and identify tracks

  # Embed chapter markers and artwork into MP3
  %(prog)s chapters recording_tracklist.md        # Auto-detect audio file
  %(prog)s chapters tracklist.md --audio set.mp3  # Specify audio file
""",
    )

    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ─────────────────────────────────────────────────────────────────────────
    # 'process' subcommand - audio processing pipeline
    # ─────────────────────────────────────────────────────────────────────────
    process_parser = subparsers.add_parser(
        "process",
        help="Process and combine audio files (join, compress, normalize)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s part1.wav part2.wav -o "My Set.mp3"
  %(prog)s *.wav -o output.mp3 --loudness -14 --bitrate 320k
  %(prog)s *.wav -o output.mp3 --identify --edit
""",
    )

    process_parser.add_argument(
        "inputs",
        nargs="+",
        help="Input audio files to process (joined in order specified)",
    )

    process_parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output file path (MP3)",
    )

    process_parser.add_argument(
        "--loudness",
        type=float,
        default=-16.0,
        help="Target loudness in LUFS (default: -16)",
    )

    process_parser.add_argument(
        "--bitrate",
        default="192k",
        help="Output bitrate (default: 192k)",
    )

    process_parser.add_argument(
        "--no-compress",
        action="store_true",
        help="Skip compression stage",
    )

    process_parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="Skip loudness normalization stage",
    )

    process_parser.add_argument(
        "--no-silence-removal",
        action="store_true",
        help="Skip leading silence removal",
    )

    process_parser.add_argument(
        "--identify",
        action="store_true",
        help="Run Shazam identification after processing",
    )

    process_parser.add_argument(
        "-e",
        "--edit",
        action="store_true",
        help="Open interactive editor after identification (requires --identify)",
    )

    process_parser.add_argument(
        "-d",
        "--delay",
        type=int,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay between Shazam API calls (default: {DEFAULT_DELAY_SECONDS})",
    )

    process_parser.add_argument(
        "--no-learn",
        action="store_true",
        help="Disable learning from corrections",
    )

    process_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show FFmpeg output",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # 'identify' subcommand - track identification (also default behavior)
    # ─────────────────────────────────────────────────────────────────────────
    identify_parser = subparsers.add_parser(
        "identify",
        help="Identify tracks in audio files using Shazam",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s recording.mp3
  %(prog)s recording.mp3 --edit
  %(prog)s set1.mp3 set2.mp3 set3.mp3
  %(prog)s /path/to/dj_sets/ -o ./tracklists/
""",
    )

    identify_parser.add_argument(
        "paths",
        nargs="+",
        help="Audio file(s), directory, or markdown tracklist to edit",
    )

    identify_parser.add_argument(
        "-o",
        "--output-dir",
        help="Output directory for tracklist files (default: same as input)",
    )

    identify_parser.add_argument(
        "-d",
        "--delay",
        type=int,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay in seconds between API calls (default: {DEFAULT_DELAY_SECONDS})",
    )

    identify_parser.add_argument(
        "-e",
        "--edit",
        action="store_true",
        help="Open interactive editor after processing",
    )

    identify_parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start fresh instead of resuming from previous progress",
    )

    identify_parser.add_argument(
        "--no-learn",
        action="store_true",
        help="Disable learning from corrections",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # 'chapters' subcommand - embed chapter markers and artwork
    # ─────────────────────────────────────────────────────────────────────────
    chapters_parser = subparsers.add_parser(
        "chapters",
        help="Embed chapter markers and artwork into an MP3 file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s recording_tracklist.md
  %(prog)s recording_tracklist.md --audio recording.mp3
  %(prog)s recording_tracklist.md --no-artwork
""",
    )

    chapters_parser.add_argument(
        "tracklist",
        help="Markdown tracklist file (from identify or editor)",
    )

    chapters_parser.add_argument(
        "--audio",
        help="Path to the MP3 file (auto-detected from tracklist name if omitted)",
    )

    chapters_parser.add_argument(
        "--no-artwork",
        action="store_true",
        help="Skip artwork fetching (embed chapter markers only)",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Parse and route
    # ─────────────────────────────────────────────────────────────────────────

    # Handle backward compatibility: if first arg is not a subcommand, treat as 'identify'
    # Check sys.argv to detect if user passed a file path directly
    if len(sys.argv) > 1:
        first_arg = sys.argv[1]
        # If first arg is not a known subcommand and not a flag, insert 'identify'
        if first_arg not in ("process", "identify", "chapters", "-h", "--help", "-v", "--version"):
            sys.argv.insert(1, "identify")

    args = parser.parse_args()

    # Handle case where no command specified (just --help or --version)
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Route to appropriate handler
    if args.command == "process":
        cmd_process(args)
    elif args.command == "identify":
        cmd_identify(args)
    elif args.command == "chapters":
        cmd_chapters(args)


if __name__ == "__main__":
    main()
