# Setlist Maker

**Generate tracklists from DJ sets using Shazam — right from your terminal.**

You just played a 2-hour set and can't remember half the tracks you played. Setlist Maker takes your recording, slices it into samples, identifies each one through Shazam, and hands you a clean tracklist. Review it in the built-in editor, and it learns from your corrections for next time.

## Features

- Identify tracks via Shazam across full-length recordings
- Review and correct results in an interactive TUI editor
- Embed chapter markers and artwork into MP3s for podcast players
- Learns from your corrections to improve future runs
- Resume interrupted sessions — progress is saved automatically
- Outputs markdown and JSON tracklists

## Quick Start

### 1. Install ffmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Windows — download from https://ffmpeg.org and add to PATH
```

### 2. Install Setlist Maker

```bash
git clone https://github.com/brigleb/setlist-maker.git
cd setlist-maker
pip install .
```

### 3. Identify your first set

```bash
setlist-maker my_set.mp3 --edit
```

That's it. Shazam identifies each track, then the interactive editor opens so you can review and fix anything it missed.

## What You Get

A markdown tracklist with timestamps:

```markdown
# Tracklist: my_set.mp3

*Generated on 2025-01-15 14:30*

1. **Artist One** - Track Title (0:00)
2. **Artist Two** - Another Track (2:30)
3. *Unidentified* (5:00)
4. **Artist Three** - Great Song (7:30)
```

When you save from the editor, a JSON file is also generated alongside:

```json
[
  {"timestamp": 0, "time": "0:00", "artist": "Artist One", "title": "Track Title"},
  {"timestamp": 150, "time": "2:30", "artist": "Artist Two", "title": "Another Track"}
]
```

## Usage

### Track Identification

The most common workflow — point it at a recording and get a tracklist:

```bash
# Identify and open the editor to review results
setlist-maker recording.mp3 --edit

# Identify without opening the editor
setlist-maker recording.mp3

# Multiple files
setlist-maker set1.mp3 set2.mp3 set3.mp3

# Entire folder
setlist-maker /path/to/dj_sets/

# Custom delay between API calls and output directory
setlist-maker /path/to/sets/ --delay 20 --output-dir ./tracklists/

# Edit an existing tracklist
setlist-maker tracklist.md
```

> **Note:** Setlist Maker expects a single, finished audio file. If your set is
> split across multiple files or needs cleanup (joining, compression, loudness
> normalization), do that in your audio editor of choice first, then run
> `identify` on the result.

### Interactive Editor

The editor gives you a spreadsheet-like interface for reviewing and correcting tracklists:

```bash
# Open editor after identification
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

### Chapter Markers & Artwork (`chapters`)

After identifying and editing a tracklist, embed it as navigable chapter markers in the MP3 — with per-chapter artwork fetched automatically:

```bash
# Embed chapters (auto-detects the audio file from tracklist name)
setlist-maker chapters my_set_tracklist.md

# Specify the audio file explicitly
setlist-maker chapters my_set_tracklist.md --audio my_set.mp3

# Chapters only, skip artwork fetching
setlist-maker chapters my_set_tracklist.md --no-artwork
```

This writes ID3v2 CHAP/CTOC frames into the MP3. Podcast players (Apple Podcasts, Overcast, Pocket Casts, etc.) and VLC will show a chapter list with timestamps, titles, and artwork for each track.

For each track, artwork is fetched using a waterfall of sources: Shazam CDN, iTunes, Deezer, and MusicBrainz/Cover Art Archive. Remix tags and featuring info are automatically stripped for smarter search fallbacks. Each chapter image gets an MTV-style lower-third overlay with the artist and title.

### Learning from Corrections

When you fix a misidentified track in the editor, Setlist Maker remembers the correction and automatically applies it in future runs. Corrections are stored in `~/.config/setlist-maker/corrections.json`.

```bash
# Disable learning for a single run
setlist-maker recording.mp3 --no-learn
```

## Options

### Identify Command

| Option | Description |
|--------|-------------|
| `-e, --edit` | Open interactive editor after processing |
| `-o, --output-dir` | Output directory for tracklist files (default: same as input) |
| `-d, --delay` | Delay in seconds between API calls (default: 15) |
| `--no-resume` | Start fresh instead of resuming from previous progress |
| `--no-learn` | Disable learning from corrections |

### Chapters Command

| Option | Description |
|--------|-------------|
| `--audio` | Path to the MP3 file (auto-detected from tracklist name if omitted) |
| `--no-artwork` | Skip artwork fetching (embed chapter markers only) |

### Global Options

| Option | Description |
|--------|-------------|
| `-v, --version` | Show version |

## How It Works

1. Loads your audio file (supports mp3, wav, flac, m4a, ogg, aac, wma, aiff)
2. Slices it into 30-second samples
3. Runs each sample through Shazam
4. Applies any learned corrections from previous sessions
5. Deduplicates consecutive matches
6. Outputs a markdown tracklist with timestamps (and JSON)

Progress is automatically saved, so if interrupted you can resume where you left off.

## License

MIT
