# Setlist Maker

Generate tracklists from DJ sets or long audio recordings using Shazam.

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

```bash
# Single file
setlist-maker recording.mp3

# Multiple files
setlist-maker set1.mp3 set2.mp3 set3.mp3

# Entire folder
setlist-maker /path/to/dj_sets/

# With options
setlist-maker /path/to/sets/ --delay 20 --output-dir ./tracklists/
```

## Options

| Option | Description |
|--------|-------------|
| `-o, --output-dir` | Output directory for tracklist files (default: same as input) |
| `-d, --delay` | Delay in seconds between API calls (default: 15) |
| `--no-resume` | Start fresh instead of resuming from previous progress |
| `-v, --version` | Show version |

## How It Works

1. Loads your audio file (supports mp3, wav, flac, m4a, ogg, aac, wma, aiff)
2. Slices it into 30-second samples
3. Runs each sample through Shazam
4. Deduplicates consecutive matches
5. Outputs a markdown tracklist with timestamps

Progress is automatically saved, so if interrupted you can resume where you left off.

## Output

Generates a markdown file like:

```markdown
# Tracklist: my_set.mp3

*Generated on 2025-01-15 14:30*

1. **~0:00** — Artist One - Track Title
2. **~2:30** — Artist Two - Another Track
3. **~5:00** — *Unidentified*
4. **~7:30** — Artist Three - Great Song
```

## License

MIT
