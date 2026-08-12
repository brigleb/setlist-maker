# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Setlist Maker is a Python CLI tool that generates tracklists from DJ sets or long audio recordings by slicing them into 30-second samples and identifying each via Shazam.

## Development Commands

```bash
# Install for development (editable mode)
pip install -e ".[dev]"

# Run the CLI
setlist-maker <audio_file_or_directory>

# Run as module
python -m setlist_maker <audio_file_or_directory>

# Lint with ruff
ruff check .
ruff format .

# Run tests
pytest
```

**System dependency:** Requires ffmpeg installed (`brew install ffmpeg` / `apt install ffmpeg`).
Its bundled `ffplay` also powers the editor's audio preview (macOS only).

## Architecture

CLI application with the following modules:

### `setlist_maker/cli.py` - Argparse entry point & command handlers
- **Entry point:** `main()` with subcommand routing (`identify`, `chapters`)
- **Backward compatible:** Running without subcommand defaults to `identify` behavior
- **Thin layer:** Owns argparse setup and the `cmd_identify` / `cmd_chapters` handlers,
  delegating real work to the modules below.

### `setlist_maker/audio.py` - Audio discovery, loading, slicing
- **get_audio_file():** Validates a single path is an existing, supported audio file
- **load_audio() / slice_audio():** Uses `pydub` to load and slice into 30-second chunks
- **Decode-completeness guard:** `load_audio()` cross-checks the decoded length against
  ffprobe's reported duration (`probe_duration_seconds()` → `verify_decode_complete()`) and
  raises `TruncatedAudioError` on a large shortfall — the signature of a file still being
  written or synced (e.g. iCloud) when read, where the OS reports the full size but only part
  has materialized. Prevents silently producing a partial tracklist. Bypass with `identify
  --allow-partial`. Tunables: `DECODE_SHORTFALL_REL_TOLERANCE`, `DECODE_SHORTFALL_MIN_GAP_SECONDS`
- `SAMPLE_DURATION_MS = 30000`; `AUDIO_EXTENSIONS` lives in `__init__.py`

### `setlist_maker/shazam_client.py` - Shazam recognition
- **identify_sample_with_retry():** Wraps `shazamio` with exponential-backoff retry for rate limits
- **estimate_confidence():** Heuristic match-confidence proxy (match alignment + corroboration),
  attached to each result as `confidence` and used by the dedup pipeline
- Constants: `MAX_RETRIES`, `INITIAL_BACKOFF`

### `setlist_maker/identify.py` - Identification pipeline
- **process_single_file():** Orchestrates slicing → recognition → dedup → summary → output
- **deduplicate_tracklist():** Fuzzy-clusters matches (normalizes remix/feat/edit tags so metadata
  drift for one track collapses), smooths isolated single-sample outliers (A B A / A None A → A),
  drops low-confidence singletons while keeping confident short tracks, then collapses consecutive
  identical tracks. Smoothing is **confidence-aware**: it only absorbs an outlier whose own sample
  confidence is below `singleton_confidence_keep`, so a real short track sandwiched between two
  longer ones survives for the singleton filter to adjudicate instead of being erased first (#7).
  A `None` dropout has no confidence to defend it and is always smoothed. The gate reads the
  *sample's* confidence, not its cluster's, so a shaky blip is still absorbed when the same track
  is confidently detected elsewhere. Tunables live in `DedupConfig` (defaults: `SIMILARITY_THRESHOLD`,
  `ARTIST_SIMILARITY_THRESHOLD`, `SINGLETON_CONFIDENCE_KEEP`) and are exposed as `identify`
  flags: `--title-threshold`, `--artist-threshold`, `--singleton-confidence`, `--no-smoothing`
- **results_to_tracklist():** Applies corrections and builds a `Tracklist`
- **Progress persistence:** `save_progress()` / `load_progress()` JSON files enable resuming
- `DEFAULT_DELAY_SECONDS = 15` (between API calls)

### `setlist_maker/summary.py` - Playlist summary generation
- **generate_summary():** Shells out to the Claude CLI (`claude -p --strict-mcp-config`) from a
  throwaway temp dir to produce a one-paragraph set description. Best-effort: returns `None`
  (warns and continues) if the CLI is missing, errors, times out, or returns nothing. On by
  default; suppressed by `identify --no-summary`. The result is stored on `Tracklist.summary`,
  rendered by `to_markdown()`, and recovered by `parse_markdown_tracklist()` for editor round-trips.

### `setlist_maker/chapters.py` + `setlist_maker/artwork.py` - Chapter markers & artwork
- **embed_chapters():** Writes ID3v2 CHAP/CTOC frames into an MP3 for podcast players.
  Saves as **ID3v2.3**, never mutagen's default v2.4 — players misparse v2.4 syncsafe
  CHAP sub-frame sizes once an artwork APIC sub-frame exceeds 128 bytes and silently
  drop every chapter (#17). Guarded by an ffprobe round-trip regression test.
- **fetch_artwork():** Waterfall lookup across Shazam CDN, iTunes, Deezer, MusicBrainz/Cover Art Archive
- Chapter composites are produced through `artwork_cache.chapter_image()`, not by
  pairing `fetch_artwork()` + `create_chapter_image()` directly — that shared cache is
  what makes the web editor's preview byte-identical to what gets embedded.
- **create_chapter_image():** Builds per-chapter artwork with an MTV-style lower-third overlay

### `setlist_maker/artwork_cache.py` - Chapter image cache
- **chapter_image():** The single path from a track to its chapter composite —
  `fetch_artwork()` + `create_chapter_image()`, cached on disk. Called by both the
  web editor's `/api/artwork` preview and `embed_chapters_for_tracklist()`, so the
  image previewed is byte-identical to the one embedded (#20).
- **Cache key is a content hash** of (artist, title, coverart_url, size), so an edit
  regenerates structurally — there is no invalidation code path. Lives in
  `$XDG_CACHE_HOME/setlist-maker/artwork` (else `~/.cache/...`).
- Per-key locks dedupe concurrent requests; a semaphore caps simultaneous generation
  at 4. An unwritable cache degrades to in-memory generation rather than failing.

### `setlist_maker/editor.py` - Interactive TUI editor
- **TracklistEditor:** Textual app providing spreadsheet-like interface
- **EditTrackScreen:** Modal dialog for editing artist/title fields
- **CorrectionsDB:** Persistent storage for user corrections (~/.config/setlist-maker/corrections.json)
- **parse_markdown_tracklist():** Parses existing markdown files for editing
- **Audio preview:** `p` previews the selected track's 30s window via `PlaybackController`
  (see `playback.py`). `_resolve_audio_path()` locates the source audio (threaded in from the
  CLI when known, else discovered beside the markdown). Playback stops on navigation/reject/edit
  and on unmount; gated by `playback_enabled` (set once in `on_mount`)

### `setlist_maker/web_editor.py` - Browser tracklist editor
- **run_web_editor():** Drop-in sibling of `editor.run_editor()` that serves a
  single-page editor (`web_editor.html`) from a loopback `ThreadingHTTPServer`
  on an ephemeral port and opens the browser. Reuses `Track`/`Tracklist`,
  `CorrectionsDB`, and the shared `save_tracklist()` / `resolve_audio_path()`
  helpers from `editor.py` so the TUI and web front ends never drift.
- **Endpoints:** `GET /` (page), `GET /api/tracklist`, `POST /api/save`
  (writes `.md` + `.json` + corrections via `save_tracklist`), `GET /api/audio`
  (HTTP Range streaming powering the in-browser scrubber), `POST /api/done`
  (graceful shutdown → returns control to the CLI).
- **Pure helpers:** `tracklist_to_api()` and `apply_edits()` are socket-free and
  unit-tested directly. `apply_edits()` maps edits onto existing tracks by stable
  `index`; an edit with **no** `index` is a track inserted via the page's per-row
  "＋ Add below" control — it's appended with its own `timestamp` and the list is
  re-sorted into chronological position (inserts aren't corrections, so none is
  recorded). The page sends existing rows by `index` (not array position, which
  inserts shift) and reloads after saving new tracks so re-save can't duplicate.
  Opened with `identify --web-edit` / `-w`; mutually exclusive with `--edit`.
- **Editable set description:** the page renders `Tracklist.summary` as an always-on,
  column-aligned `<textarea>` (empty shows an "add a description" placeholder). Its
  text rides along in the `POST /api/save` body; `_handle_save()` forwards it as
  `apply_edits(..., summary=...)`, which uses an `_UNSET` sentinel — an absent
  `summary` key leaves `tracklist.summary` untouched (so older clients can't wipe it),
  while a sent value is whitespace-normalized to one paragraph (blank/None clears it),
  keeping the `to_markdown()` ↔ `parse_markdown_tracklist()` round-trip lossless.

### `setlist_maker/playback.py` - Editor audio preview
- **PlaybackController:** Drives a non-blocking `ffplay` subprocess (`play()` / `stop()` /
  `is_playing()` / `elapsed()`), reaping the child on stop. Deliberately out-of-process: an
  earlier in-process (`sounddevice`) version locked up Textual's event loop and was removed
- **playback_available():** Capability gate — `ffplay` on PATH **and** macOS. `audio_output_available()`
  is Darwin-only (playback is unsupported elsewhere; SSH is not special-cased). `PREVIEW_SECONDS = 30`
- Seek/scrub within a track is a tracked follow-up (GitHub issue #10)

Key classes:
- `Track`: Dataclass representing a single track with timestamp, artist, title, rejected status
- `Tracklist`: Collection of tracks with markdown/JSON export methods

## Code Style

- Line length: 100 (configured in pyproject.toml)
- Ruff lint rules: E, F, W, I (errors, pyflakes, warnings, isort)
- Python 3.10+ required
