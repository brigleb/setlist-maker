# Setlist Maker

Generate tracklists from DJ sets or long audio recordings using Shazam.

## Features

- Automatic track identification via Shazam
- Interactive TUI editor for reviewing and correcting results
- Learns from your corrections to improve future identifications
- Resume interrupted processing sessions
- Multiple output formats (Markdown, JSON)

## Installation

```bash
pip install setlist-maker
```

You also need ffmpeg installed on your system:

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Windows
# Download from https://ffmpeg.org and add to PATH
```

## Usage

### Basic Usage

```bash
# Process a single file
setlist-maker recording.mp3

# Process and open interactive editor
setlist-maker recording.mp3 --edit

# Edit an existing tracklist
setlist-maker tracklist.md

# Multiple files
setlist-maker set1.mp3 set2.mp3 set3.mp3

# Entire folder
setlist-maker /path/to/dj_sets/

# With options
setlist-maker /path/to/sets/ --delay 20 --output-dir ./tracklists/
```

### Interactive Editor

The interactive editor provides a spreadsheet-like interface for reviewing and correcting tracklists:

```bash
# Open editor after processing
setlist-maker my_set.mp3 --edit

# Edit an existing tracklist file
setlist-maker my_set_tracklist.md
```

**Keyboard shortcuts:**
| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate tracks |
| `Space` | Reject/accept track |
| `Enter` | Edit artist/title |
| `S` | Save changes |
| `Q` | Quit |
| `?` | Show help |

### Learning from Corrections

Setlist Maker learns from your corrections. When you fix a misidentified track, the correction is saved and automatically applied when that same misidentification appears in future runs.

Corrections are stored in `~/.config/setlist-maker/corrections.json`.

To disable learning:
```bash
setlist-maker recording.mp3 --no-learn
```

## Options

| Option | Description |
|--------|-------------|
| `-e, --edit` | Open interactive editor after processing |
| `-o, --output-dir` | Output directory for tracklist files (default: same as input) |
| `-d, --delay` | Delay in seconds between API calls (default: 15) |
| `--no-resume` | Start fresh instead of resuming from previous progress |
| `--no-learn` | Disable learning from corrections |
| `-v, --version` | Show version |

## How It Works

1. Loads your audio file (supports mp3, wav, flac, m4a, ogg, aac, wma, aiff)
2. Slices it into 30-second samples
3. Runs each sample through Shazam
4. Applies any learned corrections from previous sessions
5. Deduplicates consecutive matches
6. Outputs a markdown tracklist with timestamps (and JSON)

Progress is automatically saved, so if interrupted you can resume where you left off.

## Output

Generates a markdown file like:

```markdown
# Tracklist: my_set.mp3

*Generated on 2025-01-15 14:30*

1. **Artist One** - Track Title (0:00)
2. **Artist Two** - Another Track (2:30)
3. *Unidentified* (5:00)
4. **Artist Three** - Great Song (7:30)
```

When saving from the interactive editor, a JSON file is also generated:

```json
[
  {"timestamp": 0, "time": "0:00", "artist": "Artist One", "title": "Track Title"},
  {"timestamp": 150, "time": "2:30", "artist": "Artist Two", "title": "Another Track"}
]
```

## License

MIT
