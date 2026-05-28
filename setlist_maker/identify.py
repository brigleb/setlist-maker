"""Track identification pipeline: Shazam batch processing, dedup, and output."""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from shazamio import Shazam

from setlist_maker.audio import (
    SAMPLE_DURATION_MS,
    format_timestamp,
    load_audio,
    slice_audio,
)
from setlist_maker.editor import CorrectionsDB, Track, Tracklist, run_editor
from setlist_maker.shazam_client import identify_sample_with_retry
from setlist_maker.summary import generate_summary

DEFAULT_DELAY_SECONDS = 15  # Pause between API calls


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

    # Add a one-paragraph playlist description ahead of the listing. Warns and
    # continues if the Claude CLI is unavailable or the call fails.
    print("  Generating playlist summary...")
    summary_lines = [
        f"{t.artist} - {t.title}"
        for t in tracklist.tracks
        if not t.rejected and not t.is_unidentified
    ]
    tracklist.summary = generate_summary(summary_lines)

    # Write markdown plus a JSON sidecar. The JSON carries each track's
    # Shazam cover-art URL, which the chapters command relies on, so it is
    # always written here -- not only when the editor saves.
    with open(output_path, "w") as f:
        f.write(tracklist.to_markdown())

    json_path = output_path.with_suffix(".json")
    with open(json_path, "w") as f:
        json.dump(tracklist.to_json(), f, indent=2)

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
