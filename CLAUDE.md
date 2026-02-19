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

Three-module CLI application:

### `setlist_maker/cli.py` - Main CLI with subcommands
- **Entry point:** `main()` with subcommand routing (`process`, `identify`)
- **Backward compatible:** Running without subcommand defaults to `identify` behavior
- **Audio identification:** Uses `pydub` to slice audio into 30-second chunks
- **Track identification:** Uses `shazamio` async library with exponential backoff retry for rate limits
- **Deduplication:** `deduplicate_tracklist()` removes singleton matches and collapses consecutive identical tracks
- **Progress persistence:** JSON progress files enable resuming interrupted runs

Key constants at top of `cli.py`:
- `SAMPLE_DURATION_MS = 30000` (30-second slices)
- `DEFAULT_DELAY_SECONDS = 15` (between API calls)
- `AUDIO_EXTENSIONS` (supported formats)

### `setlist_maker/processor.py` - FFmpeg-based audio processing
- **ProcessingConfig:** Dataclass with all processing parameters (silence threshold, compression, loudness targets)
- **process_audio():** Full pipeline: concat → silence removal → compress → normalize → MP3 export
- **build_filter_chain():** Constructs FFmpeg `-af` filter string
- **create_concat_file():** Generates filelist.txt for FFmpeg concat demuxer
- **get_audio_duration():** Uses ffprobe to get file duration
- **analyze_loudness():** Uses FFmpeg loudnorm filter to analyze loudness statistics

Default processing settings:
- Silence removal: -50dB threshold
- Compression: -18dB threshold, 3:1 ratio
- Loudness: -16 LUFS, -1.5 dBTP
- Output: MP3 CBR 192kbps

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
