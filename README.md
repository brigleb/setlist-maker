# Setlist Maker

**Generate tracklists from DJ sets using Shazam — right from your terminal.**

You just played a 2-hour set and can't remember half the tracks you played. Setlist Maker takes your recording, slices it into samples, identifies each one through Shazam, and hands you a clean tracklist. Review it in the built-in editor, and it learns from your corrections for next time.

## Features

- Identify tracks via Shazam across full-length recordings
- Join, compress, and normalize audio with a single command
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

### Audio Processing (`process`)

If your set is split across multiple files or needs cleanup before identification:

```bash
# Join multiple recordings into one
setlist-maker process part1.wav part2.wav part3.wav -o "My Set.mp3"

# Custom loudness and bitrate
setlist-maker process *.wav -o output.mp3 --loudness -14 --bitrate 320k

# Process and identify tracks in one go
setlist-maker process *.wav -o output.mp3 --identify --edit

# Skip specific processing stages
setlist-maker process recording.wav -o output.mp3 --no-compress --no-normalize
```

The processing pipeline runs these stages in order:

1. Concatenate input files (in order specified)
2. Remove leading silence (-50dB threshold)
3. Apply compression (-18dB threshold, 3:1 ratio)
4. Normalize loudness (-16 LUFS, -1.5 dBTP)
5. Export as MP3 CBR (192kbps default)

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

After identifying and editing a tracklist, embed it as navigable chapter markers in the MP3 — with per-chapter artwork fetched from Shazam/iTunes:

```bash
# Embed chapters (auto-detects the audio file from tracklist name)
setlist-maker chapters my_set_tracklist.md

# Specify the audio file explicitly
setlist-maker chapters my_set_tracklist.md --audio my_set.mp3

# Chapters only, skip artwork fetching
setlist-maker chapters my_set_tracklist.md --no-artwork
```

This writes ID3v2 CHAP/CTOC frames into the MP3. Podcast players (Apple Podcasts, Overcast, Pocket Casts, etc.) and VLC will show a chapter list with timestamps, titles, and artwork for each track.

For each track, artwork is fetched from the Shazam cover art URL saved during identification. If that fails, it falls back to the iTunes Search API. Each chapter image gets an MTV-style lower-third overlay with the artist and title.

### Learning from Corrections

When you fix a misidentified track in the editor, Setlist Maker remembers the correction and automatically applies it in future runs. Corrections are stored in `~/.config/setlist-maker/corrections.json`.

```bash
# Disable learning for a single run
setlist-maker recording.mp3 --no-learn
```

## Options

### Process Command

| Option | Description |
|--------|-------------|
| `-o, --output` | Output file path (required) |
| `--loudness` | Target loudness in LUFS (default: -16) |
| `--bitrate` | Output bitrate (default: 192k) |
| `--no-compress` | Skip compression stage |
| `--no-normalize` | Skip loudness normalization |
| `--no-silence-removal` | Skip leading silence removal |
| `--identify` | Run Shazam identification after processing |
| `-e, --edit` | Open editor after identification |
| `--verbose` | Show FFmpeg output |

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
