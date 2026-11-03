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
import re
import sys
from pathlib import Path
from typing import NoReturn

from setlist_maker import __version__
from setlist_maker.adaptive import load_probes, process_single_file_adaptive
from setlist_maker.artwork import CoverImageError, load_cover_image
from setlist_maker.audio import get_audio_file
from setlist_maker.boundary import BoundaryEngine, EngineConfig
from setlist_maker.call_log import LOG_FILENAME, call_log_path
from setlist_maker.chapters import embed_chapters_for_tracklist
from setlist_maker.editor import run_editor
from setlist_maker.help_format import ColorHelpParser
from setlist_maker.identify import (
    ARTIST_SIMILARITY_THRESHOLD,
    DEFAULT_DELAY_SECONDS,
    SIMILARITY_THRESHOLD,
    SINGLETON_CONFIDENCE_KEEP,
    DedupConfig,
    process_single_file,
    tracklist_output_path,
)
from setlist_maker.tracklist import CorrectionsDB, Tracklist, find_audio_file, load_tracklist
from setlist_maker.web_editor import WatchSession, run_web_editor

# Sourced from EngineConfig so the flags, their help text and the epilog can
# never drift from what the engine actually defaults to.
_ENGINE_DEFAULTS = EngineConfig()
_D_PRECISION = _ENGINE_DEFAULTS.precision
_D_STRIDE = _ENGINE_DEFAULTS.stride
_D_REFINE = _ENGINE_DEFAULTS.refine_window

_BUDGET_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([hms]?)\s*$", re.IGNORECASE)


def parse_budget(text: str) -> float:
    """'2h' / '45m' / '90s' / bare minutes -> seconds. Raises ValueError."""
    match = _BUDGET_RE.match(text or "")
    if not match:
        raise ValueError(f"invalid duration: {text!r} (try 45m, 2h or 90s)")
    value, unit = float(match.group(1)), match.group(2).lower()
    return value * {"h": 3600.0, "m": 60.0, "s": 1.0}.get(unit or "m")


# argparse `type=` callables. Raising ArgumentTypeError is what makes argparse
# print the message itself ("argument --stride: must be greater than 0 ...") and
# exit 2; any other exception becomes a bare "invalid <name> value".


def _budget(text: str) -> float:
    try:
        return parse_budget(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None


def _unit_interval(text: str) -> float:
    value = _number(text)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"must be between 0.0 and 1.0 (got {value})")
    return value


def _positive(text: str) -> float:
    value = _number(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0 (got {value})")
    return value


def _fail(message: str) -> NoReturn:
    """A runtime failure, as opposed to a usage error: stderr and exit 1."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _check_identify(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """The usage rules that span two flags, which argparse can't express itself."""
    if args.cover and not args.chapters:
        parser.error("--cover sets the episode cover for chapter embedding; add --chapters")
    if args.stride <= args.refine_window:
        parser.error(
            f"--stride ({args.stride}) must exceed --refine-window "
            f"({args.refine_window}); coverage has to be coarser than refinement"
        )


def _chain_chapters_after_identify(
    output_path: Path,
    audio_path: Path | None,
    fetch_art: bool,
    cover_image: bytes | None = None,
) -> None:
    """
    Embed chapters into the processed MP3 after `identify --chapters`.

    Reloads the tracklist from its saved files so any edits made in the
    editor (and the JSON sidecar's cover-art URLs) are picked up.
    """
    tracklist = load_tracklist(output_path)
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
    embed_chapters_for_tracklist(
        tracklist,
        audio_path,
        fetch_art=fetch_art,
        cover_image=cover_image,
        tracklist_path=output_path,
    )


def _resolve_cover(cover_arg: str | None) -> bytes | None:
    """Turn a --cover path into embeddable JPEG bytes, or exit with a clear error.

    Shared by `chapters` and the `identify --chapters` chain. Failing here rather
    than mid-embed matters: chapter writing mutates the MP3 in place, so a bad
    path should stop the run before anything is touched.
    """
    if not cover_arg:
        return None
    cover_path = Path(cover_arg)
    if not cover_path.exists():
        _fail(f"Cover image not found: {cover_path}")
    try:
        data = load_cover_image(cover_path)
    except CoverImageError as e:
        _fail(str(e))
    print(f"  Cover image: {cover_path.name}")
    return data


def _resolve_call_log(
    args: argparse.Namespace, audio_path: Path, output_dir: Path | None
) -> Path | None:
    """Where this run's per-call telemetry goes, or None when it is switched off.

    On by default: the log exists to be reviewed across several ordinary runs,
    and one that had to be enabled per run would be empty exactly when a
    throttling question came up. `--no-call-log` outranks an explicit
    `--call-log`, so the off switch is never overridden by a stale path.
    """
    if getattr(args, "no_call_log", False):
        return None
    explicit = getattr(args, "call_log", None)
    if explicit:
        return Path(explicit)
    return call_log_path(audio_path, output_dir)


def _resume_hint(audio_path: Path, output_dir: Path | None, args: argparse.Namespace) -> str | None:
    """Describe unfinished adaptive work saved beside a reusable tracklist.

    A stopped adaptive run *always* leaves a tracklist behind -- that is the
    anytime property working as designed -- so the reuse short-circuit fires on
    the normal way of continuing one, and the flag that continues it is called
    `--reidentify`. Read cold, that name says the opposite of what it does
    here: it bypasses the *tracklist* reuse while still resuming from the saved
    probes (`--no-resume` is what discards them). Saying so is the difference
    between a 13-minute continuation and an hour of re-scanning nobody asked
    for.

    Best-effort in the spirit of `summary.py`: any unreadable or unexpected
    progress file yields no hint rather than derailing a path whose whole job
    is to hand back an existing tracklist.
    """
    if getattr(args, "no_resume", False) or getattr(args, "sequential", False):
        return None
    output_path = tracklist_output_path(audio_path, output_dir)
    progress_path = output_path.with_name(f"{audio_path.stem}_progress.json")
    if not progress_path.exists():
        return None
    try:
        probes, duration = load_probes(progress_path)
        if not probes:
            return None
        if duration is None:
            # A legacy sequential file. It converts and resumes, but carries no
            # duration, so there is nothing to size the remaining work against.
            return (
                f"  {len(probes)} saved sample(s) from an earlier run would be reused.\n"
                f"  --reidentify continues from them (--no-resume starts over)."
            )
        engine = BoundaryEngine(
            duration,
            EngineConfig(
                stride=args.stride,
                precision=args.precision,
                refine_window=args.refine_window,
                singleton_confidence_keep=args.singleton_confidence,
                title_threshold=args.title_threshold,
                artist_threshold=args.artist_threshold,
            ),
        )
        for probe in probes:
            engine.add_probe(probe)
        remaining = engine.estimated_probes_remaining()
    except Exception:
        return None
    if not remaining:
        return None  # converged: the tracklist beside it is the finished answer
    plural = "" if len(probes) == 1 else "s"
    return (
        f"  An unfinished adaptive run is also saved: {len(probes)} probe{plural}, "
        f"~{remaining} remaining.\n"
        f"  --reidentify continues it (--no-resume discards it and starts over)."
    )


def cmd_identify(args: argparse.Namespace) -> None:
    """Handle the 'identify' subcommand (default behavior)."""
    input_path = Path(args.path)

    # Flag combinations and ranges were checked while parsing (_check_identify,
    # the type= validators and the editor-mode group), so they fail before here.
    # Resolve up front so a bad path fails before a long identification pass,
    # not after it
    cover_image = _resolve_cover(args.cover) if args.chapters else None

    # Editing an existing markdown tracklist
    if input_path.suffix.lower() == ".md" and input_path.is_file():
        print(f"Opening tracklist for editing: {input_path.name}")
        # Same loader the chapters path uses: it parses this markdown for
        # structure *and* picks up the JSON sidecar's coverart_url. Parsing the
        # markdown alone would leave coverart_url None, so the editor would
        # preview a differently-keyed composite than `chapters` embeds.
        tracklist = load_tracklist(input_path)
        if not tracklist.tracks:
            _fail("Could not parse tracklist from markdown file.")
        print(f"Loaded {len(tracklist.tracks)} tracks from {tracklist.source_file}")
        # --watch on a finished tracklist has no run to watch: it is --web-edit.
        editor_fn = run_web_editor if (args.web_edit or args.watch) else run_editor
        editor_fn(tracklist, input_path, use_corrections=not args.no_learn)

        if args.chapters:
            _chain_chapters_after_identify(
                input_path, None, fetch_art=not args.no_artwork, cover_image=cover_image
            )
        return

    budget_seconds = args.budget

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
    watch = None
    if output_path.exists() and not args.reidentify:
        existing = load_tracklist(output_path)
        if existing.tracks:
            print(
                f"Found existing tracklist: {output_path.name} "
                f"(use --reidentify to regenerate from audio)"
            )
            hint = _resume_hint(audio_path, output_dir, args)
            if hint:
                print(hint)
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

        engine_config = EngineConfig(
            stride=args.stride,
            precision=args.precision,
            refine_window=args.refine_window,
            singleton_confidence_keep=args.singleton_confidence,
            title_threshold=args.title_threshold,
            artist_threshold=args.artist_threshold,
        )
        # Opened before the run so the page can follow it from the first probe.
        # A sequential run has no way to take a user's question between its
        # samples, so it is watched without click-to-sample.
        watch = (
            WatchSession(
                audio_path,
                output_path,
                engine_config=None if args.sequential else engine_config,
                sampling=not args.sequential,
            )
            if args.watch
            else None
        )
        try:
            result = _run_identification(
                args,
                audio_path,
                output_dir,
                corrections_db,
                dedup_config,
                engine_config,
                budget_seconds,
                watch,
            )
        except BaseException:
            if watch is not None:
                watch.stop()
            raise

        if not result:
            if watch is not None:
                watch.stop()
            print()
            _fail(f"Failed to process {audio_path.name}")

        tracklist, output_path = result

    print(f"\n{'─' * 40}")
    print(tracklist.to_markdown())

    if watch is not None:
        # The page that watched the run becomes its editor, in the same tab.
        watch.finish(
            tracklist, output_path, use_corrections=not args.no_learn, audio_path=audio_path
        )
    elif args.edit or args.web_edit or args.watch:
        web = args.web_edit or args.watch
        editor_fn = run_web_editor if web else run_editor
        kind = "browser" if web else "interactive"
        print(f"\nOpening {kind} editor for: {tracklist.source_file}")
        editor_fn(
            tracklist,
            output_path,
            use_corrections=not args.no_learn,
            audio_path=audio_path,
        )

    if args.chapters:
        _chain_chapters_after_identify(
            output_path, audio_path, fetch_art=not args.no_artwork, cover_image=cover_image
        )


def _run_identification(
    args: argparse.Namespace,
    audio_path: Path,
    output_dir: Path | None,
    corrections_db: CorrectionsDB | None,
    dedup_config: DedupConfig,
    engine_config: EngineConfig,
    budget_seconds: float | None,
    watch: WatchSession | None,
) -> tuple[Tracklist, Path] | None:
    """Run whichever driver the flags select; returns its ``(tracklist, path)`` or None."""
    if args.sequential:
        return asyncio.run(
            process_single_file(
                audio_path=audio_path,
                output_dir=output_dir,
                delay_seconds=args.delay,
                resume=not args.no_resume,
                corrections_db=corrections_db,
                dedup_config=dedup_config,
                summary=not args.no_summary,
                allow_partial=args.allow_partial,
                panel=not args.no_panel,
                call_log=_resolve_call_log(args, audio_path, output_dir),
            )
        )
    if args.no_smoothing:
        print(
            "  Note: --no-smoothing applies to --sequential only; "
            "adaptive mode adjudicates outliers by re-probing."
        )
    return asyncio.run(
        process_single_file_adaptive(
            audio_path=audio_path,
            output_dir=output_dir,
            delay_seconds=args.delay,
            engine_config=engine_config,
            resume=not args.no_resume,
            corrections_db=corrections_db,
            summary=not args.no_summary,
            allow_partial=args.allow_partial,
            panel=not args.no_panel,
            budget_seconds=budget_seconds,
            call_log=_resolve_call_log(args, audio_path, output_dir),
            requests=watch.requests if watch is not None else None,
        )
    )


def cmd_chapters(args: argparse.Namespace) -> None:
    """Handle the 'chapters' subcommand for embedding chapter markers."""
    tracklist_path = Path(args.tracklist)

    if not tracklist_path.exists():
        _fail(f"Tracklist file not found: {tracklist_path}")

    # Load tracklist with any saved artwork URLs
    print(f"Loading tracklist: {tracklist_path.name}")
    tracklist = load_tracklist(tracklist_path)

    # Require at least one identified track to build a meaningful chapter list
    if not any(not t.is_unidentified for t in tracklist.tracks if not t.rejected):
        _fail("No identified tracks found in tracklist.")

    chapter_count = len([t for t in tracklist.tracks if not t.rejected])
    print(f"  Found {chapter_count} tracks")

    # Find the audio file
    if args.audio:
        audio_path = Path(args.audio)
    else:
        audio_path = find_audio_file(tracklist_path)

    if not audio_path or not audio_path.exists():
        _fail("Could not find the audio file.\n  Use --audio to specify the MP3 file path.")

    if audio_path.suffix.lower() != ".mp3":
        _fail(f"Chapter markers require an MP3 file, got: {audio_path.suffix}")

    print(f"  Audio file: {audio_path.name}")

    cover_image = _resolve_cover(args.cover)
    embed_chapters_for_tracklist(
        tracklist,
        audio_path,
        fetch_art=not args.no_artwork,
        cover_image=cover_image,
        tracklist_path=tracklist_path,
    )


def main():
    # Short aliases keep the help epilog's f-string lines within the line limit
    # while still sourcing every default from its canonical constant.
    d_delay = DEFAULT_DELAY_SECONDS
    d_title = SIMILARITY_THRESHOLD
    d_artist = ARTIST_SIMILARITY_THRESHOLD
    d_single = SINGLETON_CONFIDENCE_KEEP
    d_precision = _D_PRECISION
    d_stride = _D_STRIDE
    d_refine = _D_REFINE

    parser = ColorHelpParser(
        prog="setlist-maker",
        description="Generate tracklists from DJ sets or long audio recordings using Shazam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Setlist Maker hunts track boundaries adaptively: it samples a long recording
sparsely, notices where two samples disagree, and spends the rest of its probes
narrowing exactly those disagreements. Boundaries land within ~{d_precision:.0f}s for
roughly half the Shazam calls the old fixed 30-second scan needed (still there,
as --sequential). Output is a timestamped markdown tracklist plus a JSON sidecar.

Stop it whenever you like -- Ctrl-C, or --budget 45m. The tracklist is always
complete; more probes only make the boundaries sharper.

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
      --watch                 Watch the run on a browser timeline; it becomes the
                              web editor when the run finishes
  -o, --output-dir DIR        Where to write tracklist files (default: beside input)
  -d, --delay SECONDS         Pause between Shazam calls (default: {d_delay})
      --chapters              Embed chapters + artwork after identifying (and editing)
      --no-artwork            With --chapters, embed markers only (skip artwork)
      --reidentify            Regenerate from audio even if a tracklist exists
      --no-resume             Ignore saved progress and start fresh
      --allow-partial         Process even if far less audio decodes than reported
      --no-learn              Don't read or save corrections
      --no-summary            Skip the Claude-generated set summary (on by default)
      --no-panel              Don't pin the live progress panel under the log
      --call-log PATH         Where to append per-call telemetry (default: beside output)
      --no-call-log           Don't record per-call telemetry
  adaptive sampling
      --sequential            Use the old fixed 30s scan (more calls, +-30s boundaries)
      --budget DURATION       Stop after about this long and write what was found
      --precision SECONDS     How tightly to pin each boundary (default: {d_precision:g})
      --stride SECONDS        Longest unprobed stretch (default: {d_stride:g}); guarantees
                              no track longer than this is missed
      --refine-window SECONDS Sample length when narrowing a boundary (default: {d_refine:g})
  detection tuning
      --title-threshold N       Title similarity 0-1 to merge matches (default: {d_title})
      --artist-threshold N      Artist similarity 0-1 to merge matches (default: {d_artist})
      --singleton-confidence N  Min confidence 0-1 to keep a 1-sample track (default: {d_single})
      --no-smoothing            Don't smooth unconfident single-sample outliers (A B A -> A)
                                (--sequential only; adaptive re-probes outliers instead)

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
  %(prog)s recording.mp3 --budget 45m          Spend at most ~45 minutes on it
  %(prog)s recording.mp3 --sequential          Use the old fixed 30-second scan
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

    # One editor per run: --watch already opens the browser editor at the end.
    editor_mode = identify_parser.add_mutually_exclusive_group()
    editor_mode.add_argument(
        "-e",
        "--edit",
        action="store_true",
        help="Open the editor after identifying — or directly on an existing "
        "tracklist for this audio, if one is found",
    )

    editor_mode.add_argument(
        "-w",
        "--web-edit",
        action="store_true",
        dest="web_edit",
        help="Open the editor in your browser instead of the terminal",
    )

    editor_mode.add_argument(
        "--watch",
        action="store_true",
        help="Follow the run on a timeline in your browser -- click it to ask Shazam "
        "about any moment -- and edit there when it finishes (implies --web-edit)",
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
        "--cover",
        metavar="IMAGE",
        help="With --chapters, use this image file as the episode cover instead of "
        "the derived one (per-track chapter images are unaffected)",
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

    identify_parser.add_argument(
        "--call-log",
        metavar="PATH",
        help=f"Where to append per-call telemetry (default: {LOG_FILENAME} beside the output)",
    )

    identify_parser.add_argument(
        "--no-call-log",
        action="store_true",
        help="Don't record per-call telemetry",
    )

    identify_parser.add_argument(
        "--no-panel",
        action="store_true",
        help="Don't pin the live progress panel under the log "
        "(it is already skipped when output is piped or redirected)",
    )

    # Adaptive sampling (see EngineConfig in boundary.py)
    adaptive_group = identify_parser.add_argument_group("adaptive sampling")
    adaptive_group.add_argument(
        "--sequential",
        action="store_true",
        help="Use the original fixed 30-second scan instead of adaptive boundary "
        "hunting (more Shazam calls, boundaries only accurate to +-30s)",
    )
    adaptive_group.add_argument(
        "--precision",
        type=_positive,
        default=_D_PRECISION,
        metavar="SECONDS",
        help=f"How tightly to pin each track boundary (default: {_D_PRECISION}); "
        "lower spends more probes per boundary",
    )
    adaptive_group.add_argument(
        "--budget",
        type=_budget,
        metavar="DURATION",
        help="Stop after roughly this long and write what has been found "
        "(e.g. 45m, 2h, 90s; a bare number means minutes). The result is always "
        "a complete tracklist -- just a coarser one (default: run to completion)",
    )
    adaptive_group.add_argument(
        "--stride",
        type=_positive,
        default=_D_STRIDE,
        metavar="SECONDS",
        help=f"Longest stretch left unprobed between samples (default: {_D_STRIDE}); "
        "this is what guarantees no track longer than it is missed, so raising it "
        "saves calls at the risk of skipping a short track",
    )
    adaptive_group.add_argument(
        "--refine-window",
        type=_positive,
        default=_D_REFINE,
        dest="refine_window",
        metavar="SECONDS",
        help=f"Sample length used when narrowing a boundary (default: {_D_REFINE}); "
        "Shazam fingerprints a centered 10s excerpt of whatever it is given",
    )

    # Deduplication tuning (see DedupConfig in identify.py)
    tuning_group = identify_parser.add_argument_group("detection tuning")
    tuning_group.add_argument(
        "--title-threshold",
        type=_unit_interval,
        default=SIMILARITY_THRESHOLD,
        metavar="0.0-1.0",
        help="Title similarity (0-1) required to merge two matches as the same "
        f"track (default: {SIMILARITY_THRESHOLD}); lower merges more aggressively",
    )
    tuning_group.add_argument(
        "--artist-threshold",
        type=_unit_interval,
        default=ARTIST_SIMILARITY_THRESHOLD,
        metavar="0.0-1.0",
        help="Artist similarity (0-1) required to merge two matches as the same "
        f"track (default: {ARTIST_SIMILARITY_THRESHOLD}); keeps different artists apart",
    )
    tuning_group.add_argument(
        "--singleton-confidence",
        type=_unit_interval,
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

    chapters_parser.add_argument(
        "--cover",
        metavar="IMAGE",
        help="Use this image file as the episode cover instead of the derived "
        "one (per-track chapter images are unaffected)",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Parse and route
    # ─────────────────────────────────────────────────────────────────────────

    # Backward compatibility: no subcommand means 'identify', so `setlist-maker
    # set.mp3 --edit` and `setlist-maker --edit set.mp3` both work. Both lists
    # come from the parser, so a new subcommand or top-level flag needs no edit here.
    top_level_flags = {flag for action in parser._actions for flag in action.option_strings}
    if len(sys.argv) > 1 and sys.argv[1] not in {*subparsers.choices, *top_level_flags}:
        sys.argv.insert(1, "identify")

    args = parser.parse_args()

    # Handle case where no command specified (just --help or --version)
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Route to appropriate handler
    if args.command == "identify":
        _check_identify(identify_parser, args)
        cmd_identify(args)
    elif args.command == "chapters":
        cmd_chapters(args)


if __name__ == "__main__":
    main()
