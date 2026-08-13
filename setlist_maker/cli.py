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

    # Re-running on the same audio reuses the saved tracklist (no re-Shazam);
    # this reopens it in the editor. Add --reidentify to regenerate from audio.
    setlist-maker recording.mp3 --edit

    # Edit an existing tracklist directly
    setlist-maker recording_tracklist.md

    # Embed chapter markers and artwork into an MP3
    setlist-maker chapters recording_tracklist.md
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from setlist_maker import __version__
from setlist_maker.artwork import create_chapter_image
from setlist_maker.artwork_cache import chapter_image, source_artwork, used_fallback
from setlist_maker.audio import get_audio_file
from setlist_maker.chapters import embed_chapters
from setlist_maker.editor import (
    CorrectionsDB,
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
    process_single_file,
    tracklist_output_path,
)
from setlist_maker.web_editor import run_web_editor


def _chain_chapters_after_identify(
    output_path: Path,
    audio_path: Path | None,
    fetch_art: bool,
) -> None:
    """
    Embed chapters into the processed MP3 after `identify --chapters`.

    Reloads the tracklist from its saved files so any edits made in the
    editor (and the JSON sidecar's cover-art URLs) are picked up.
    """
    tracklist, _urls = _load_tracklist_with_artwork_urls(output_path)
    if audio_path is None or not audio_path.exists():
        audio_path = find_audio_file(output_path)

    if not audio_path or not audio_path.exists():
        print(f"\nSkipping chapters for {tracklist.source_file}: audio file not found.")
        return
    if audio_path.suffix.lower() != ".mp3":
        print(
            f"\nSkipping chapters for {audio_path.name}: chapter markers require an MP3 "
            f"(got {audio_path.suffix})."
        )
        return
    if not any(not t.is_unidentified for t in tracklist.tracks if not t.rejected):
        print(f"\nSkipping chapters for {audio_path.name}: no identified tracks.")
        return

    print(f"\n{'=' * 60}")
    print(f"Embedding chapters into: {audio_path.name}")
    print(f"{'=' * 60}")
    embed_chapters_for_tracklist(tracklist, audio_path, fetch_art=fetch_art)


def cmd_identify(args: argparse.Namespace) -> None:
    """Handle the 'identify' subcommand (default behavior)."""
    input_path = Path(args.path)

    if args.edit and args.web_edit:
        print("Error: choose either --edit (terminal) or --web-edit (browser), not both.")
        sys.exit(1)

    # Editing an existing markdown tracklist
    if input_path.suffix.lower() == ".md" and input_path.is_file():
        print(f"Opening tracklist for editing: {input_path.name}")
        # Same loader the chapters path uses: it parses this markdown for
        # structure *and* picks up the JSON sidecar's coverart_url. Parsing the
        # markdown alone would leave coverart_url None, so the editor would
        # preview a differently-keyed composite than `chapters` embeds.
        tracklist, _urls = _load_tracklist_with_artwork_urls(input_path)
        if not tracklist.tracks:
            print("Error: Could not parse tracklist from markdown file.")
            sys.exit(1)
        print(f"Loaded {len(tracklist.tracks)} tracks from {tracklist.source_file}")
        editor_fn = run_web_editor if args.web_edit else run_editor
        editor_fn(tracklist, input_path, use_corrections=not args.no_learn)

        if args.chapters:
            _chain_chapters_after_identify(input_path, None, fetch_art=not args.no_artwork)
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

    # Identify a single audio file
    audio_path = get_audio_file(args.path)
    if not audio_path:
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else None
    output_path = tracklist_output_path(audio_path, output_dir)

    # Reuse a tracklist already generated for this audio instead of re-running
    # Shazam, unless --reidentify asks for a fresh pass. A file that parses to
    # zero tracks (corrupt/empty) is treated as absent and regenerated.
    tracklist = None
    if output_path.exists() and not args.reidentify:
        existing, _urls = _load_tracklist_with_artwork_urls(output_path)
        if existing.tracks:
            print(
                f"Found existing tracklist: {output_path.name} "
                f"(use --reidentify to regenerate from audio)"
            )
            tracklist = existing

    if tracklist is None:
        print(f"Processing: {audio_path.name}")
        corrections_db = CorrectionsDB() if not args.no_learn else None

        # Build deduplication tuning config from flags (validated above)
        dedup_config = DedupConfig(
            title_threshold=args.title_threshold,
            artist_threshold=args.artist_threshold,
            singleton_confidence_keep=args.singleton_confidence,
            smoothing=not args.no_smoothing,
        )

        result = asyncio.run(
            process_single_file(
                audio_path=audio_path,
                output_dir=output_dir,
                delay_seconds=args.delay,
                resume=not args.no_resume,
                corrections_db=corrections_db,
                dedup_config=dedup_config,
                summary=not args.no_summary,
                allow_partial=args.allow_partial,
            )
        )

        if not result:
            print(f"\nError: Failed to process {audio_path.name}")
            sys.exit(1)

        tracklist, output_path = result

    print(f"\n{'─' * 40}")
    print(tracklist.to_markdown())

    if args.edit or args.web_edit:
        editor_fn = run_web_editor if args.web_edit else run_editor
        kind = "browser" if args.web_edit else "interactive"
        print(f"\nOpening {kind} editor for: {tracklist.source_file}")
        editor_fn(
            tracklist,
            output_path,
            use_corrections=not args.no_learn,
            audio_path=audio_path,
        )

    if args.chapters:
        _chain_chapters_after_identify(output_path, audio_path, fetch_art=not args.no_artwork)


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

            # One cached path shared with the editor's preview, so the image
            # embedded here is byte-identical to the one the user approved.
            chapter_images[i] = chapter_image(
                artist=track.artist,
                title=track.title,
                coverart_url=track.coverart_url,
            )

            # Episode cover: first track with *real* artwork, relabelled for the
            # set. used_fallback() preserves the pre-cache behavior of skipping
            # tracks whose composite is just the gradient.
            if episode_image is None and not used_fallback(
                track.artist, track.title, track.coverart_url
            ):
                # Same fetched art as this track's chapter image (normally a
                # cache hit, no network), relabelled for the set as a whole.
                # Only accept it if real source bytes actually came back: a
                # cached composite whose .src is gone (an older build, a
                # disk-full window between the two writes, a pruned cache) can
                # still report "had real art" while the re-fetch fails. Feeding
                # that None to create_chapter_image() would yield a gradient
                # *and* latch it, blocking every later track with real art.
                src = source_artwork(track.artist, track.title, track.coverart_url)
                if src:
                    episode_image = create_chapter_image(
                        artwork_bytes=src,
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
    # Short aliases keep the help epilog's f-string lines within the line limit
    # while still sourcing every default from its canonical constant.
    d_delay = DEFAULT_DELAY_SECONDS
    d_title = SIMILARITY_THRESHOLD
    d_artist = ARTIST_SIMILARITY_THRESHOLD
    d_single = SINGLETON_CONFIDENCE_KEEP

    parser = argparse.ArgumentParser(
        prog="setlist-maker",
        description="Generate tracklists from DJ sets or long audio recordings using Shazam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Setlist Maker samples a long recording every 30 seconds, identifies each slice
with Shazam, and writes a timestamped markdown tracklist (plus a JSON sidecar).

Typical workflow
  1. Identify + review   %(prog)s my_set.mp3 --edit   (or --web-edit in a browser)
  2. ...or all at once   %(prog)s my_set.mp3 --edit --chapters
  3. Add chapters later  %(prog)s chapters my_set_tracklist.md

  Run an audio file through `identify` to get the tracklist, open the editor
  with --edit to fix any misses (corrections are remembered next time), then
  embed chapter markers + cover artwork into the MP3 for podcast players.

  Re-running on the same audio reuses the saved tracklist (no re-Shazam), so
  `%(prog)s my_set.mp3 --edit` reopens it for editing. Pass --reidentify to
  regenerate from the audio instead.

Commands
  identify <audio | tracklist.md>   Identify tracks  (default; may be omitted)
  chapters <tracklist.md>           Embed chapter markers + artwork into an MP3

identify options
  -e, --edit                  Open the editor (on an existing tracklist if found)
  -w, --web-edit              Open the editor in your browser instead of the TUI
  -o, --output-dir DIR        Where to write tracklist files (default: beside input)
  -d, --delay SECONDS         Pause between Shazam calls (default: {d_delay})
      --chapters              Embed chapters + artwork after identifying (and editing)
      --no-artwork            With --chapters, embed markers only (skip artwork)
      --reidentify            Regenerate from audio even if a tracklist exists
      --no-resume             Ignore saved progress and start fresh
      --allow-partial         Process even if far less audio decodes than reported
      --no-learn              Don't read or save corrections
      --no-summary            Skip the Claude-generated set summary (on by default)
  detection tuning
      --title-threshold N       Title similarity 0-1 to merge matches (default: {d_title})
      --artist-threshold N      Artist similarity 0-1 to merge matches (default: {d_artist})
      --singleton-confidence N  Min confidence 0-1 to keep a 1-sample track (default: {d_single})
      --no-smoothing            Don't smooth unconfident single-sample outliers (A B A -> A)

chapters options
      --audio FILE            MP3 path (auto-detected from the tracklist name if omitted)
      --no-artwork            Embed chapter markers only (skip artwork)

global options
  -h, --help                  Show help; use `%(prog)s identify -h` for full detail
  -v, --version               Show version

Examples
  %(prog)s recording.mp3                       Identify tracks (or reuse if done)
  %(prog)s recording.mp3 --edit                Identify (or reuse), then edit
  %(prog)s recording.mp3 --web-edit            Identify (or reuse), then edit in a browser
  %(prog)s recording.mp3 --reidentify --edit   Force a fresh re-identify, then edit
  %(prog)s recording.mp3 --edit --chapters     Identify, edit, then add chapters
  %(prog)s tracklist.md                        Edit an existing tracklist
  %(prog)s chapters recording_tracklist.md     Add chapters to the matching MP3
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
  %(prog)s recording.mp3 --web-edit
  %(prog)s recording.mp3 --edit --chapters   # identify, edit, then embed chapters
  %(prog)s recording.mp3 -o ./tracklists/
""",
    )

    identify_parser.add_argument(
        "path",
        help="A single audio file, or a markdown tracklist to edit",
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
        help="Open the editor after identifying — or directly on an existing "
        "tracklist for this audio, if one is found",
    )

    identify_parser.add_argument(
        "-w",
        "--web-edit",
        action="store_true",
        dest="web_edit",
        help="Open the editor in your browser instead of the terminal "
        "(cannot be combined with --edit)",
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
        "--reidentify",
        action="store_true",
        help="Re-run identification from the audio even if a tracklist already "
        "exists (by default the existing tracklist is reused); combine with "
        "--no-resume for a full cold re-scan",
    )

    identify_parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start fresh instead of resuming from previous progress",
    )

    identify_parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Skip the decode-completeness check and process whatever decodes "
        "(by default a file that decodes far shorter than it reports is rejected, "
        "since it is usually still being written or synced)",
    )

    identify_parser.add_argument(
        "--no-learn",
        action="store_true",
        help="Disable learning from corrections",
    )

    identify_parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Skip the Claude-generated playlist summary paragraph "
        "(on by default; requires the 'claude' CLI)",
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
        f"(default: {SINGLETON_CONFIDENCE_KEEP}); higher drops more one-off matches, "
        "and also lets smoothing absorb more of them",
    )
    tuning_group.add_argument(
        "--no-smoothing",
        action="store_true",
        help="Disable smoothing of isolated single-sample outliers (A B A -> A); "
        "smoothing already spares outliers above --singleton-confidence",
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
