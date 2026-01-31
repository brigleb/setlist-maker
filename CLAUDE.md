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

Single-module CLI application with all logic in `setlist_maker/cli.py`:

- **Entry point:** `main()` parses args and kicks off `process_batch()` async
- **Audio processing:** Uses `pydub` to load and slice audio into 30-second chunks
- **Track identification:** Uses `shazamio` async library with exponential backoff retry for rate limits
- **Deduplication:** `deduplicate_tracklist()` removes singleton matches (likely samples) and collapses consecutive identical tracks
- **Progress persistence:** JSON progress files enable resuming interrupted runs

Key constants at top of `cli.py`:
- `SAMPLE_DURATION_MS = 30000` (30-second slices)
- `DEFAULT_DELAY_SECONDS = 15` (between API calls)
- `AUDIO_EXTENSIONS` (supported formats)

## Code Style

- Line length: 100 (configured in pyproject.toml)
- Ruff lint rules: E, F, W, I (errors, pyflakes, warnings, isort)
- Python 3.10+ required
