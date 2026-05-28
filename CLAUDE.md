# CLAUDE.md

## MANDATORY: Use td for Task Management

You must run td usage --new-session at conversation start (or after /clear) to see current work.
Use td usage -q for subsequent reads.

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

## Architecture

CLI application with the following modules:

### `setlist_maker/cli.py` - Argparse entry point & command handlers
- **Entry point:** `main()` with subcommand routing (`identify`, `chapters`)
- **Backward compatible:** Running without subcommand defaults to `identify` behavior
- **Thin layer:** Owns argparse setup and the `cmd_identify` / `cmd_chapters` handlers,
  delegating real work to the modules below.

### `setlist_maker/audio.py` - Audio discovery, loading, slicing
- **get_audio_files():** Expands files/directories into the list of audio files to process
- **load_audio() / slice_audio():** Uses `pydub` to load and slice into 30-second chunks
- `SAMPLE_DURATION_MS = 30000`; `AUDIO_EXTENSIONS` lives in `__init__.py`

### `setlist_maker/shazam_client.py` - Shazam recognition
- **identify_sample_with_retry():** Wraps `shazamio` with exponential-backoff retry for rate limits
- **estimate_confidence():** Heuristic match-confidence proxy (match alignment + corroboration),
  attached to each result as `confidence` and used by the dedup pipeline
- Constants: `MAX_RETRIES`, `INITIAL_BACKOFF`

### `setlist_maker/identify.py` - Identification pipeline
- **process_batch() / process_single_file():** Orchestrate slicing → recognition → output
- **deduplicate_tracklist():** Fuzzy-clusters matches (normalizes remix/feat/edit tags so metadata
  drift for one track collapses), smooths isolated single-sample outliers (A B A / A None A → A),
  drops low-confidence singletons while keeping confident short tracks, then collapses consecutive
  identical tracks. Tunables: `SIMILARITY_THRESHOLD`, `ARTIST_SIMILARITY_THRESHOLD`,
  `SINGLETON_CONFIDENCE_KEEP`
- **results_to_tracklist():** Applies corrections and builds a `Tracklist`
- **Progress persistence:** `save_progress()` / `load_progress()` JSON files enable resuming
- `DEFAULT_DELAY_SECONDS = 15` (between API calls)

### `setlist_maker/chapters.py` + `setlist_maker/artwork.py` - Chapter markers & artwork
- **embed_chapters():** Writes ID3v2 CHAP/CTOC frames into an MP3 for podcast players
- **fetch_artwork():** Waterfall lookup across Shazam CDN, iTunes, Deezer, MusicBrainz/Cover Art Archive
- **create_chapter_image():** Builds per-chapter artwork with an MTV-style lower-third overlay

### `setlist_maker/editor.py` - Interactive TUI editor
- **TracklistEditor:** Textual app providing spreadsheet-like interface
- **EditTrackScreen:** Modal dialog for editing artist/title fields
- **CorrectionsDB:** Persistent storage for user corrections (~/.config/setlist-maker/corrections.json)
- **parse_markdown_tracklist():** Parses existing markdown files for editing

Key classes:
- `Track`: Dataclass representing a single track with timestamp, artist, title, rejected status
- `Tracklist`: Collection of tracks with markdown/JSON export methods

## Code Style

- Line length: 100 (configured in pyproject.toml)
- Ruff lint rules: E, F, W, I (errors, pyflakes, warnings, isort)
- Python 3.10+ required
