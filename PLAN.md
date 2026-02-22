# Podcast Metadata & Artwork Plan

## Overview

Add the ability to export the final MP3 with embedded podcast metadata:
chapter markers (so listeners can skip between tracks), episode artwork,
and per-chapter artwork showing the currently playing track.

---

## What the ID3v2 spec gives us

MP3 files support two key ID3v2 frame types for this:

- **CHAP** (Chapter) frames — each defines a time range in the audio with an
  element ID, start/end time in ms, and optional sub-frames (title, artwork, URL)
- **CTOC** (Table of Contents) — a root frame listing all CHAP element IDs
- **APIC** (Attached Picture) — episode-level cover art (type 3 = front cover),
  and also embeddable as a sub-frame inside each CHAP for per-chapter artwork

### Player support

Chapters with per-chapter artwork are supported by: **Apple Podcasts** (iOS),
**Overcast**, **Pocket Casts**, **Castro**, **AntennaPod**, **Player FM**, and
most other podcast apps. Spotify uses its own proprietary chapter system and
ignores ID3 chapters. VLC also displays them.

---

## Proposed features

### 1. Episode-level artwork (`--artwork`)

Embed a single cover image in the MP3 as the episode artwork (APIC frame,
picture type 3). This is what podcast apps show in the player and episode list.

- Accept a path to a JPEG/PNG image file via `--artwork cover.jpg`
- Recommended: 600×600 pixels, JPEG, under 200 KB for maximum compatibility
  (some Android/set-top devices choke on large ID3 artwork)
- Embedded as an APIC frame with `type=3` (front cover)

### 2. Chapter markers from tracklist (`--chapters`)

After identification + editing, embed CHAP/CTOC frames in the MP3 so each
identified track becomes a navigable chapter.

- Each non-rejected track becomes a CHAP frame
- Start time = track's timestamp (already known)
- End time = next track's timestamp (or audio duration for the last track)
- TIT2 sub-frame = "Artist - Title"
- CTOC frame lists all chapters, marked as top-level + ordered

### 3. Per-chapter artwork (two approaches)

#### Option A: Fetched cover art

For each track, fetch the album/track artwork and embed it as an APIC
sub-frame inside that track's CHAP frame.

- **Source: Shazam API** — The response already includes artwork URLs at
  `track.images.coverart` (hosted on `mzstatic.com`, default 400×400).
  We just need to capture this URL during identification (currently ignored).
- **Fallback: iTunes Search API** — Free, no auth required:
  `https://itunes.apple.com/search?term=artist+title&entity=song&limit=1`
  Returns `artworkUrl100` which can be resized (replace `100x100` with `600x600`).
- Downloaded, resized to 600×600, converted to JPEG, embedded in the CHAP frame.
- Cache images locally to avoid re-downloading on re-runs.

#### Option B: Text-overlay artwork ("MTV style")

Take the episode cover art (or a dark background), and render the artist name
and track title as text in the bottom-left corner — like an MTV/VH1 lower-third.

- Use **Pillow** (PIL) to composite text onto the base image
- Semi-transparent dark gradient band at bottom for readability
- Artist in bold/white, title below in lighter weight
- Generate one image per chapter, embed as APIC sub-frame in each CHAP
- Advantage: consistent visual style, no external API calls needed
- Works even when no cover art is available for a track

#### Recommended: Option A with Option B as fallback

Fetch real cover art when available; for tracks where artwork fetch fails,
fall back to the text-overlay approach using the episode artwork as the base.

---

## Implementation plan

### New dependencies

```
mutagen>=1.47.0    # ID3 tag manipulation (CHAP, CTOC, APIC frames)
Pillow>=10.0.0     # Image processing for text overlay artwork
requests>=2.28.0   # HTTP requests for fetching artwork (or use urllib)
```

### New module: `setlist_maker/metadata.py`

Core functions:

```python
def embed_episode_artwork(mp3_path, image_path):
    """Embed episode-level cover art as APIC frame (type 3)."""

def embed_chapters(mp3_path, tracklist, audio_duration_ms):
    """Write CHAP + CTOC frames from a Tracklist."""

def embed_chapter_artwork(mp3_path, chapter_id, image_data):
    """Add APIC sub-frame to an existing CHAP frame."""

def fetch_track_artwork(artist, title, artwork_url=None):
    """Fetch artwork: try artwork_url first, fall back to iTunes Search API."""

def generate_overlay_artwork(base_image_path, artist, title, size=(600, 600)):
    """Render artist/title text over base image, MTV lower-third style."""
```

### Changes to existing code

1. **`cli.py` identify_sample_with_retry()** — Also capture
   `track.images.coverart` from the Shazam response and store in the result dict.

2. **`editor.py` Track dataclass** — Add `artwork_url: str | None = None` field.

3. **`editor.py` action_save() / Tracklist.to_json()** — Include `artwork_url`
   in JSON export.

4. **`cli.py` argument parser** — Add new flags to the `process` and `identify`
   subcommands:
   - `--artwork PATH` — Episode cover art image
   - `--chapters` — Embed chapter markers from tracklist
   - `--chapter-art` — Fetch/generate per-chapter artwork
   - `--chapter-art-style {fetch,overlay,both}` — Which approach to use

5. **`cli.py` post-save flow** — After the editor saves (or after batch
   processing), if `--chapters` is set, call the metadata module to embed
   chapters into the MP3.

### New subcommand: `tag`

Alternatively, add a `tag` subcommand that can be run independently:

```
setlist-maker tag recording.mp3 recording_tracklist.json \
  --artwork cover.jpg \
  --chapters \
  --chapter-art
```

This would let users tag an existing MP3 with an existing tracklist JSON,
without re-running identification. Useful for re-tagging with different
artwork or after manual tracklist edits.

---

## Technical details

### Mutagen chapter writing (sketch)

```python
from mutagen.id3 import ID3, CTOC, CHAP, TIT2, APIC, CTOCFlags

tags = ID3(mp3_path)

# Episode artwork
tags.add(APIC(encoding=0, mime='image/jpeg', type=3,
              desc='Cover', data=image_bytes))

# Table of contents
child_ids = [f"chp{i}" for i in range(len(tracks))]
tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
              child_element_ids=child_ids, sub_frames=[TIT2(text=["Tracklist"])]))

# Individual chapters
for i, track in enumerate(tracks):
    start_ms = track.timestamp * 1000
    end_ms = next_timestamp_ms  # or audio_duration_ms for last track
    sub_frames = [TIT2(text=[f"{track.artist} - {track.title}"])]
    if chapter_artwork_bytes:
        sub_frames.append(APIC(encoding=0, mime='image/jpeg', type=0,
                               desc=f'Chapter {i}', data=chapter_artwork_bytes))
    tags.add(CHAP(element_id=f"chp{i}", start_time=start_ms, end_time=end_ms,
                  sub_frames=sub_frames))

tags.save()
```

### Pillow text overlay (sketch)

```python
from PIL import Image, ImageDraw, ImageFont

def generate_overlay_artwork(base_path, artist, title, size=(600, 600)):
    img = Image.open(base_path).resize(size)
    draw = ImageDraw.Draw(img, 'RGBA')

    # Semi-transparent gradient band at bottom
    band_height = 120
    for y in range(size[1] - band_height, size[1]):
        alpha = int(180 * (y - (size[1] - band_height)) / band_height)
        draw.rectangle([(0, y), (size[0], y)], fill=(0, 0, 0, alpha))

    # Text
    font_artist = ImageFont.truetype("Arial Bold", 28)
    font_title = ImageFont.truetype("Arial", 22)
    draw.text((20, size[1] - 80), artist, font=font_artist, fill='white')
    draw.text((20, size[1] - 45), title, font=font_title, fill=(200, 200, 200))

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return buf.getvalue()
```

### iTunes Search API for artwork fallback

```python
import urllib.request, json

def fetch_itunes_artwork(artist, title):
    query = urllib.parse.quote(f"{artist} {title}")
    url = f"https://itunes.apple.com/search?term={query}&entity=song&limit=1"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    if data["resultCount"] > 0:
        art_url = data["results"][0]["artworkUrl100"].replace("100x100", "600x600")
        with urllib.request.urlopen(art_url) as resp:
            return resp.read()
    return None
```

---

## Open questions for discussion

1. **Standalone `tag` subcommand vs. integrated into existing flow?**
   A `tag` subcommand is more flexible (can re-tag without re-identifying),
   but integrating into the existing `--edit` flow is more seamless.
   Recommendation: do both — `tag` subcommand + `--chapters` flag on `identify`.

2. **Font handling for text overlays**: System fonts vary. Options:
   - Bundle a small open-source font (e.g., Inter, Roboto) — adds ~200KB
   - Use Pillow's built-in bitmap font (ugly but guaranteed to work)
   - Let user specify font path via config

3. **Artwork caching**: Store fetched artwork in
   `~/.cache/setlist-maker/artwork/` keyed by artist-title hash?

4. **Should chapter artwork be opt-in or default?** Fetching artwork for
   every track adds network calls and processing time. Chapter markers
   themselves are lightweight and could default to on.
