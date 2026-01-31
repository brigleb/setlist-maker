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

## Architecture

Two-module CLI application:

### `setlist_maker/cli.py` - Main CLI and audio processing
- **Entry point:** `main()` parses args, detects file type (audio vs markdown)
- **Audio processing:** Uses `pydub` to load and slice audio into 30-second chunks
- **Track identification:** Uses `shazamio` async library with exponential backoff retry for rate limits
- **Deduplication:** `deduplicate_tracklist()` removes singleton matches and collapses consecutive identical tracks
- **Progress persistence:** JSON progress files enable resuming interrupted runs

Key constants at top of `cli.py`:
- `SAMPLE_DURATION_MS = 30000` (30-second slices)
- `DEFAULT_DELAY_SECONDS = 15` (between API calls)
- `AUDIO_EXTENSIONS` (supported formats)

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
