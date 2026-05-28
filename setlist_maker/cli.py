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
    - Embed chapter markers and per-track artwork into MP3s

Requirements:
    pip install setlist-maker

You also need ffmpeg installed on your system:
    macOS: brew install ffmpeg
    Ubuntu/Debian: sudo apt install ffmpeg
    Windows: download from ffmpeg.org and add to PATH

Usage:
    # Identify tracks and open the interactive editor
    setlist-maker recording.mp3 --edit

    # Identify without opening the editor
    setlist-maker recording.mp3

    # Edit an existing tracklist
    setlist-maker tracklist.md

    # Multiple files or a whole directory
    setlist-maker set1.mp3 set2.mp3 set3.mp3
    setlist-maker /path/to/sets/ --delay 20 --output-dir ./tracklists/

    # Embed chapter markers and artwork into an MP3
    setlist-maker chapters recording_tracklist.md
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from setlist_maker import AUDIO_EXTENSIONS, __version__
from setlist_maker.artwork import create_chapter_image, fetch_artwork
from setlist_maker.audio import get_audio_files
from setlist_maker.chapters import embed_chapters
from setlist_maker.editor import (
    Tracklist,
    find_audio_file,
    parse_markdown_tracklist,
    run_editor,
)
from setlist_maker.identify import (
    ARTIST_SIMILARITY_THRESHOLD,
    DEFAULT_DELAY_SECONDS,
    SIMILARITY_THRESHOLD,
    SINGLETON_CONFIDENCE_KEEP,
    DedupConfig,
    process_batch,
)


def _chain_chapters_after_identify(
    results: list[tuple[Tracklist, Path]],
    audio_files: list[Path],
    fetch_art: bool,
) -> None:
    """
    Embed chapters into each processed MP3 after `identify --chapters`.

    Reloads each tracklist from its saved files so any edits made in the
    editor (and the JSON sidecar's cover-art URLs) are picked up.
    """
    audio_by_name = {f.name: f for f in audio_files}

    for _tracklist, output_path in results:
        tracklist, _urls = _load_tracklist_with_artwork_urls(output_path)
        audio_path = audio_by_name.get(tracklist.source_file) or find_audio_file(output_path)

        if not audio_path or not audio_path.exists():
            print(f"\nSkipping chapters for {tracklist.source_file}: audio file not found.")
            continue
        if audio_path.suffix.lower() != ".mp3":
            print(
                f"\nSkipping chapters for {audio_path.name}: chapter markers require an MP3 "
                f"(got {audio_path.suffix})."
            )
            continue
        if not any(not t.is_unidentified for t in tracklist.tracks if not t.rejected):
            print(f"\nSkipping chapters for {audio_path.name}: no identified tracks.")
            continue

        print(f"\n{'=' * 60}")
        print(f"Embedding chapters into: {audio_path.name}")
        print(f"{'=' * 60}")
        embed_chapters_for_tracklist(tracklist, audio_path, fetch_art=fetch_art)


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

            if args.chapters:
                _chain_chapters_after_identify(
                    [(tracklist, input_path)], audio_files=[], fetch_art=not args.no_artwork
                )
            return

    # Validate deduplication tuning flags up front so a bad value fails fast
    for name, value in (
        ("--title-threshold", args.title_threshold),
        ("--artist-threshold", args.artist_threshold),
        ("--singleton-confidence", args.singleton_confidence),
    ):
        if not 0.0 <= value <= 1.0:
            print(f"Error: {name} must be between 0.0 and 1.0 (got {value}).")
            sys.exit(1)

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

    # Build deduplication tuning config from flags (validated above)
    dedup_config = DedupConfig(
        title_threshold=args.title_threshold,
        artist_threshold=args.artist_threshold,
        singleton_confidence_keep=args.singleton_confidence,
        smoothing=not args.no_smoothing,
    )

    # Run the batch processor
    results = asyncio.run(
        process_batch(
            audio_files=audio_files,
            output_dir=output_dir,
            delay_seconds=args.delay,
            resume=not args.no_resume,
            open_editor=args.edit,
            use_corrections=not args.no_learn,
            dedup_config=dedup_config,
        )
    )

    # Optionally chain into chapter embedding
    if args.chapters:
        _chain_chapters_after_identify(results, audio_files, fetch_art=not args.no_artwork)


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

            # Map coverart URLs from JSON to tracklist tracks by timestamp
            # (index mapping breaks when rejected tracks are excluded from JSON)
            json_by_timestamp = {jt["timestamp"]: jt for jt in json_tracks if "timestamp" in jt}
            for i, track in enumerate(tracklist.tracks):
                jt = json_by_timestamp.get(track.timestamp)
                if jt:
                    url = jt.get("coverart_url")
                    if url:
                        track.coverart_url = url
                        coverart_urls[i] = url

            return tracklist, coverart_urls
        except (json.JSONDecodeError, IOError):
            pass

    # Fallback: parse markdown only (no coverart URLs available)
    with open(tracklist_path) as f:
        tracklist = parse_markdown_tracklist(f.read())

    return tracklist, coverart_urls


def embed_chapters_for_tracklist(
    tracklist: Tracklist,
    audio_path: Path,
    fetch_art: bool = True,
) -> None:
    """
    Embed chapter markers (and, optionally, artwork) into an MP3 for a tracklist.

    Shared by the `chapters` command and the `identify --chapters` chain.
    Assumes audio_path is a validated, existing MP3 and the tracklist has at
    least one identified track.
    """
    # Get all non-rejected tracks (including unidentified) for chapter timing
    chapter_tracks = [t for t in tracklist.tracks if not t.rejected]

    # Fetch artwork and generate chapter images
    chapter_images: dict[int, bytes] = {}
    episode_image: bytes | None = None

    if fetch_art:
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
        chapter_images=chapter_images if fetch_art else None,
        episode_image=episode_image if fetch_art else None,
    )

    print(f"\n  Embedded {len(chapter_tracks)} chapter(s) into {audio_path.name}")
    if chapter_images:
        print(f"  Embedded {len(chapter_images)} chapter image(s)")
    if episode_image:
        print("  Embedded episode cover art")

    print(f"\n{'=' * 60}")
    print("Done! Chapter markers embedded successfully.")
    print(f"{'=' * 60}")


def cmd_chapters(args: argparse.Namespace) -> None:
    """Handle the 'chapters' subcommand for embedding chapter markers."""
    tracklist_path = Path(args.tracklist)

    if not tracklist_path.exists():
        print(f"Error: Tracklist file not found: {tracklist_path}")
        sys.exit(1)

    # Load tracklist with any saved artwork URLs
    print(f"Loading tracklist: {tracklist_path.name}")
    tracklist, _coverart_urls = _load_tracklist_with_artwork_urls(tracklist_path)

    # Require at least one identified track to build a meaningful chapter list
    if not any(not t.is_unidentified for t in tracklist.tracks if not t.rejected):
        print("Error: No identified tracks found in tracklist.")
        sys.exit(1)

    chapter_count = len([t for t in tracklist.tracks if not t.rejected])
    print(f"  Found {chapter_count} tracks")

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

    embed_chapters_for_tracklist(tracklist, audio_path, fetch_art=not args.no_artwork)


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

  # Embed chapter markers and artwork into MP3
  %(prog)s chapters recording_tracklist.md        # Auto-detect audio file
  %(prog)s chapters tracklist.md --audio set.mp3  # Specify audio file
""",
    )

    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

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
  %(prog)s recording.mp3 --edit --chapters   # identify, edit, then embed chapters
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
        "--chapters",
        action="store_true",
        help="Embed chapter markers and artwork into each MP3 after identifying "
        "(and editing, if --edit is also used)",
    )

    identify_parser.add_argument(
        "--no-artwork",
        action="store_true",
        help="With --chapters, embed chapter markers only (skip artwork fetching)",
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

    # Deduplication tuning (see DedupConfig in identify.py)
    tuning_group = identify_parser.add_argument_group("detection tuning")
    tuning_group.add_argument(
        "--title-threshold",
        type=float,
        default=SIMILARITY_THRESHOLD,
        metavar="0.0-1.0",
        help="Title similarity (0-1) required to merge two matches as the same "
        f"track (default: {SIMILARITY_THRESHOLD}); lower merges more aggressively",
    )
    tuning_group.add_argument(
        "--artist-threshold",
        type=float,
        default=ARTIST_SIMILARITY_THRESHOLD,
        metavar="0.0-1.0",
        help="Artist similarity (0-1) required to merge two matches as the same "
        f"track (default: {ARTIST_SIMILARITY_THRESHOLD}); keeps different artists apart",
    )
    tuning_group.add_argument(
        "--singleton-confidence",
        type=float,
        default=SINGLETON_CONFIDENCE_KEEP,
        metavar="0.0-1.0",
        help="Min Shazam confidence (0-1) to keep a track seen in only one sample "
        f"(default: {SINGLETON_CONFIDENCE_KEEP}); higher drops more one-off matches",
    )
    tuning_group.add_argument(
        "--no-smoothing",
        action="store_true",
        help="Disable smoothing of isolated single-sample outliers (A B A -> A)",
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
        if first_arg not in ("identify", "chapters", "-h", "--help", "-v", "--version"):
            sys.argv.insert(1, "identify")

    args = parser.parse_args()

    # Handle case where no command specified (just --help or --version)
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Route to appropriate handler
    if args.command == "identify":
        cmd_identify(args)
    elif args.command == "chapters":
        cmd_chapters(args)


if __name__ == "__main__":
    main()
