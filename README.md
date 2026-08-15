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

> **Optional:** if the [`claude` CLI](https://claude.com/claude-code) is installed and
> authenticated, each tracklist is prefaced with a short, AI-generated paragraph
> describing the set's genres, sound, and mood. It's on by default and degrades
> gracefully — if `claude` isn't found the summary is simply skipped. Turn it off
> with `--no-summary`.

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

A propulsive late-night house set that leans on deep, dubby basslines and warm
analog pads, building from hypnotic minimalism into brighter, vocal-driven peaks.

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

# Custom delay between API calls and output directory
setlist-maker recording.mp3 --delay 20 --output-dir ./tracklists/

# Edit an existing tracklist
setlist-maker tracklist.md

# The whole pipeline in one go: identify, edit, then embed chapters
setlist-maker recording.mp3 --edit --chapters
```

Every run writes both a markdown tracklist and a JSON sidecar (the JSON carries
each track's Shazam cover-art URL, so the `chapters` command can reuse it later).

Artwork previewed in the web editor is cached on disk (`$XDG_CACHE_HOME/setlist-maker/artwork`,
or `~/.cache/setlist-maker/artwork` if `XDG_CACHE_HOME` isn't set) and reused by
`chapters`, so what you approve on screen is exactly what gets embedded — and a
`--chapters` run right after editing needs no network.

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

### Editing in the browser

Prefer a point-and-click UI? Use `--web-edit` (`-w`) instead of `--edit`:

```bash
setlist-maker recording.mp3 --web-edit
```

This opens a small editor in your browser, served from a local-only
(`127.0.0.1`) server. You can reject tracks, edit artist/title inline, and
preview audio with a real scrubber (no ffplay/macOS requirement — it works
anywhere a browser does). **Save** writes the same `.md` + `.json` outputs;
**Done** closes the server and returns to the CLI (so `--web-edit --chapters`
still chains). `--edit` and `--web-edit` cannot be combined.

### Chapter Markers & Artwork (`chapters`)

After identifying and editing a tracklist, embed it as navigable chapter markers in the MP3 — with per-chapter artwork fetched automatically:

```bash
# Embed chapters (auto-detects the audio file from tracklist name)
setlist-maker chapters my_set_tracklist.md

# Specify the audio file explicitly
setlist-maker chapters my_set_tracklist.md --audio my_set.mp3

# Chapters only, skip artwork fetching
setlist-maker chapters my_set_tracklist.md --no-artwork

# Use your own image as the episode cover
setlist-maker chapters my_set_tracklist.md --cover artwork/keys-lounge.jpg
```

You can also skip the separate step and chain chapter embedding directly onto
`identify` with `--chapters` (it runs after the editor closes, if `--edit` is
used). Chapters require an MP3; non-MP3 inputs are skipped with a notice.

```bash
setlist-maker my_set.mp3 --edit --chapters
```

This writes ID3v2 CHAP/CTOC frames into the MP3. Podcast players (Apple Podcasts, Overcast, Pocket Casts, etc.) and VLC will show a chapter list with timestamps, titles, and artwork for each track.

Tags are written as ID3v2.3 — the ecosystem convention for podcast chapters. Players widely misparse ID3v2.4's syncsafe CHAP sub-frame sizes once artwork pushes a sub-frame past 128 bytes, silently hiding every chapter (see issue #17).

For each track, artwork is fetched using a waterfall of sources: Shazam CDN, iTunes, Deezer, and MusicBrainz/Cover Art Archive. Remix tags and featuring info are automatically stripped for smarter search fallbacks. Each chapter image gets an MTV-style lower-third overlay with the artist and title.

#### Choosing your own episode cover

By default the episode cover is the first track's artwork, relabelled with the
set name. To use your own image instead — a poster, a flyer, a photo — pass
`--cover`:

```bash
setlist-maker chapters my_set_tracklist.md --cover artwork/keys-lounge.jpg
```

Your image is used as-is, with **no lower-third overlay** — a cover you picked
is finished art, not a generated chapter card. Non-square images are
center-cropped rather than squashed. Per-track chapter artwork is untouched, so
every track still shows its own album art as it plays.

It also works with `--no-artwork`, if you want your cover but no per-track
lookups, and on the `identify` chain:

```bash
setlist-maker my_set.mp3 -w --chapters --cover artwork/keys-lounge.jpg
```

### Finalizing a set

Once the tracklist looks right in the editor — correct titles, artwork you're
happy with — here's the path to a finished MP3.

**In the browser, click Save before Done.** Done closes the editor and warns
about unsaved changes, but it does not save them. Everything downstream reads
from disk.

**If you launched with `--chapters`, you're finished** — clicking Done chains
straight into embedding:

```bash
setlist-maker my_set.mp3 -w --chapters
```

**Otherwise, run `chapters` when you're done editing:**

```bash
setlist-maker chapters my_set_tracklist.md
```

This is fast and holds no surprises: `chapters` builds each image through the
same cached path the browser preview used, so the artwork you approved is
byte-identical to the artwork embedded, and it's cache hits rather than fresh
lookups.

Worth knowing before you run it:

- **It writes into the MP3 in place.** Tags only — the audio is untouched — but
  no copy is made, so back up first if that matters.
- **Rejected tracks are dropped** from the chapter list entirely.
- **Unidentified tracks you didn't reject still get a chapter marker** at their
  timestamp, just no image.
- Add `--cover` for your own episode cover (above), or `--no-artwork` for
  markers only.

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
| `--chapters` | Embed chapter markers and artwork into each MP3 after identifying (and editing) |
| `--no-artwork` | With `--chapters`, embed chapter markers only (skip artwork fetching) |
| `--cover` | With `--chapters`, use this image as the episode cover instead of the first track's artwork |
| `-o, --output-dir` | Output directory for tracklist files (default: same as input) |
| `-d, --delay` | Delay in seconds between API calls (default: 15) |
| `--no-resume` | Start fresh instead of resuming from previous progress |
| `--no-learn` | Disable learning from corrections |
| `--no-summary` | Skip the Claude-generated playlist summary (on by default; requires the `claude` CLI) |

### Chapters Command

| Option | Description |
|--------|-------------|
| `--audio` | Path to the MP3 file (auto-detected from tracklist name if omitted) |
| `--no-artwork` | Skip artwork fetching (embed chapter markers only) |
| `--cover` | Use this image as the episode cover instead of the first track's artwork |

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
6. Generates a one-paragraph set description via the `claude` CLI (unless `--no-summary`)
7. Outputs a markdown tracklist with timestamps (and JSON)

Progress is automatically saved, so if interrupted you can resume where you left off.

## Development

Requires **Python 3.10–3.13**. (Python 3.14 is not yet supported — a native
dependency, `shazamio_core`, has no compatible wheel and crashes on import.)

```bash
git clone https://github.com/brigleb/setlist-maker.git
cd setlist-maker

# Create a virtualenv on a supported Python and install with dev extras
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Lint and test
ruff check .
ruff format --check .
pytest
```

## License

MIT
