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
Its bundled `ffplay` also powers the editor's audio preview (macOS only).

## Architecture

CLI application with the following modules:

### `setlist_maker/cli.py` - Argparse entry point & command handlers
- **Entry point:** `main()` with subcommand routing (`identify`, `chapters`)
- **Backward compatible:** Running without subcommand defaults to `identify` behavior
- **Thin layer:** Owns argparse setup and the `cmd_identify` / `cmd_chapters` handlers,
  delegating real work to the modules below.

### `setlist_maker/audio.py` - Audio discovery, loading, slicing
- **get_audio_file():** Validates a single path is an existing, supported audio file
- **load_audio() / slice_audio():** Uses `pydub` to load and slice into 30-second chunks
- **Decode-completeness guard:** `load_audio()` cross-checks the decoded length against
  ffprobe's reported duration (`probe_duration_seconds()` → `verify_decode_complete()`) and
  raises `TruncatedAudioError` on a large shortfall — the signature of a file still being
  written or synced (e.g. iCloud) when read, where the OS reports the full size but only part
  has materialized. Prevents silently producing a partial tracklist. Bypass with `identify
  --allow-partial`. Tunables: `DECODE_SHORTFALL_REL_TOLERANCE`, `DECODE_SHORTFALL_MIN_GAP_SECONDS`
- `SAMPLE_DURATION_MS = 30000`; `AUDIO_EXTENSIONS` lives in `__init__.py`

### `setlist_maker/shazam_client.py` - Shazam recognition
- **identify_sample_with_retry():** Wraps `shazamio` with exponential-backoff retry for rate limits
- **estimate_confidence():** Heuristic match-confidence proxy (match alignment + corroboration),
  attached to each result as `confidence` and used by the dedup pipeline
- Constants: `MAX_RETRIES`, `INITIAL_BACKOFF`

### `setlist_maker/identify.py` - Identification pipeline
- **process_single_file():** Orchestrates slicing → recognition → dedup → summary → output
- **deduplicate_tracklist():** Fuzzy-clusters matches (normalizes remix/feat/edit tags so metadata
  drift for one track collapses), smooths isolated single-sample outliers (A B A / A None A → A),
  drops low-confidence singletons while keeping confident short tracks, then collapses consecutive
  identical tracks. Smoothing is **confidence-aware**: it only absorbs an outlier whose own sample
  confidence is below `singleton_confidence_keep`, so a real short track sandwiched between two
  longer ones survives for the singleton filter to adjudicate instead of being erased first (#7).
  A `None` dropout has no confidence to defend it and is always smoothed. The gate reads the
  *sample's* confidence, not its cluster's, so a shaky blip is still absorbed when the same track
  is confidently detected elsewhere. Tunables live in `DedupConfig` (defaults: `SIMILARITY_THRESHOLD`,
  `ARTIST_SIMILARITY_THRESHOLD`, `SINGLETON_CONFIDENCE_KEEP`) and are exposed as `identify`
  flags: `--title-threshold`, `--artist-threshold`, `--singleton-confidence`, `--no-smoothing`
- **results_to_tracklist():** Applies corrections and builds a `Tracklist`
- **Progress persistence:** `save_progress()` / `load_progress()` JSON files enable resuming
- `DEFAULT_DELAY_SECONDS = 15` (between API calls)

### `setlist_maker/summary.py` - Playlist summary generation
- **generate_summary():** Shells out to the Claude CLI (`claude -p --strict-mcp-config`) from a
  throwaway temp dir to produce a one-paragraph set description. Best-effort: returns `None`
  (warns and continues) if the CLI is missing, errors, times out, or returns nothing. On by
  default; suppressed by `identify --no-summary`. The result is stored on `Tracklist.summary`,
  rendered by `to_markdown()`, and recovered by `parse_markdown_tracklist()` for editor round-trips.

### `setlist_maker/chapters.py` + `setlist_maker/artwork.py` - Chapter markers & artwork
- **embed_chapters():** Writes ID3v2 CHAP/CTOC frames into an MP3 for podcast players.
  Saves as **ID3v2.3**, never mutagen's default v2.4 — players misparse v2.4 syncsafe
  CHAP sub-frame sizes once an artwork APIC sub-frame exceeds 128 bytes and silently
  drop every chapter (#17). Guarded by an ffprobe round-trip regression test.
  After `save()`, `_order_chap_frames_chronologically()` rewrites the tag so the CHAP
  frames sit in time order: mutagen sorts every frame by serialized size at save time
  (only APIC keeps insertion order), so differing chapter artwork scatters them, and
  while conforming players follow `CTOC`, ffprobe/ffmpeg — and hosts and web players
  built on them — enumerate CHAP frames in file order and show a shuffled list (#33).
  mutagen has no ordering hook and all its serialization is private, so this permutes
  frames in the saved bytes instead: the tag is a fixed-size region, so the audio never
  moves, and any layout it doesn't expect (other version, tag/frame flags, short frame)
  leaves the file exactly as mutagen wrote it. Guarded by `TestChapterFrameOrder`, whose
  artwork *shrinks* per chapter so an unfixed embed writes them in exact reverse.
- **fetch_artwork():** Waterfall lookup across Shazam CDN, iTunes, Deezer, MusicBrainz/Cover Art Archive
- **create_chapter_image():** Builds per-chapter artwork with an MTV-style lower-third overlay
- **load_cover_image():** Normalizes a user-supplied episode cover (`--cover`) for embedding —
  center-crop to square (`create_chapter_image()` hard-resizes instead, which squashes), then
  the shared `_compress_to_jpeg()`. Deliberately skips the lower-third overlay: hand-picked
  cover art is finished, not a generated chapter card. Raises `CoverImageError`.
  `embed_chapters_for_tracklist()` seeds `episode_image` with it, which both uses it and
  short-circuits the first-track derivation (already guarded on "still `None`"). Independent
  of `fetch_art`, so `--cover --no-artwork` embeds just the cover.

### `setlist_maker/artwork_cache.py` - Chapter image cache
- **chapter_image():** The single path from a track to its chapter composite —
  `source_artwork()` + `create_chapter_image()`, cached on disk. Called by both the
  web editor's `/api/artwork` preview and `embed_chapters_for_tracklist()`, so the
  image previewed is byte-identical to the one embedded (#20).
- **source_artwork():** The one place a track's artwork is resolved, caching *both*
  answers — the fetched bytes in `<key>.src`, or an empty `<key>.fallback` marker when
  the lookup came back empty. The marker is a **negative-result cache**: it stops a
  track with nothing findable from re-running the six-request waterfall every run, and
  being a file it survives across processes, so a later `chapters` run (a fresh process,
  cache hits only) still knows. Caching the source separately from the composite is what
  lets the episode cover reuse a track's fetched art under different overlay text with
  no second lookup. A `None` return means "no artwork for this track" and is the
  caller's to interpret — `embed_chapters_for_tracklist()` uses it to pick the episode
  cover from the first track with *real* art rather than the first identified one.
  Cached bytes win over a marker if both somehow exist.
- **Cache key is a content hash** of (artist, title, coverart_url, size), so an edit
  regenerates structurally — there is no invalidation code path. Lives in
  `$XDG_CACHE_HOME/setlist-maker/artwork` (else `~/.cache/...`). Every loader that
  feeds the editor or `chapters` must supply `coverart_url` (the JSON sidecar's, via
  `_load_tracklist_with_artwork_urls()`) — parsing markdown alone leaves it `None` and
  silently keys the preview differently from the embed.
  Regeneration is necessary but **not sufficient** on a correction: `fetch_artwork()`
  tries `coverart_url` ahead of every search, so a corrected track whose stale Shazam
  URL survived would re-key, re-fetch, and composite the *same wrong cover* under the
  new text. `editor.apply_track_edit()` clearing the URL is what makes the new key
  resolve to new art (#30).
- Per-key locks — `RLock`, since `chapter_image()` calls `source_artwork()` for the
  *same* key while still holding its own lock — dedupe concurrent requests; a
  semaphore caps simultaneous network fetches at 4 (compositing is local PIL work and
  is not capped). An unwritable cache degrades to in-memory generation rather than
  failing.

### `setlist_maker/editor.py` - Interactive TUI editor
- **TracklistEditor:** Textual app providing spreadsheet-like interface
- **EditTrackScreen:** Modal dialog for editing artist/title fields
- **CorrectionsDB:** Persistent storage for user corrections (~/.config/setlist-maker/corrections.json)
- **apply_track_edit():** The one place a correction lands on a `Track` — shared with the
  web editor (like `save_tracklist()`) so the two front ends cannot drift. Stamps
  `original_*` once, records the correction, and **clears `coverart_url`**: that URL is
  evidence attached to the *original* identification, and leaving it attached pins the
  chapter art to the misidentified track (#30). No-ops when nothing changed, so
  rejecting a row — which re-sends its unchanged fields — keeps art that is still right.
- **parse_markdown_tracklist():** Parses existing markdown files for editing
- **Audio preview:** `p` previews the selected track's 30s window via `PlaybackController`
  (see `playback.py`). `_resolve_audio_path()` locates the source audio (threaded in from the
  CLI when known, else discovered beside the markdown). Playback stops on navigation/reject/edit
  and on unmount; gated by `playback_enabled` (set once in `on_mount`)

### `setlist_maker/web_editor.py` - Browser tracklist editor
- **run_web_editor():** Drop-in sibling of `editor.run_editor()` that serves a
  single-page editor (`web_editor.html`) from a loopback `ThreadingHTTPServer`
  on an ephemeral port and opens the browser. Reuses `Track`/`Tracklist`,
  `CorrectionsDB`, and the shared `save_tracklist()` / `resolve_audio_path()`
  helpers from `editor.py` so the TUI and web front ends never drift.
- **Endpoints:** `GET /` (page), `GET /api/tracklist`, `POST /api/save`
  (writes `.md` + `.json` + corrections via `save_tracklist`), `GET /api/audio`
  (HTTP Range streaming powering the in-browser scrubber), `GET /api/artwork?index=N`
  (the JPEG chapter composite for one track, from `artwork_cache.chapter_image()` —
  the exact bytes `chapters` will embed; served `Cache-Control: no-store` since the
  page re-requests after a save. 404s on an unparseable or out-of-range `index`, and
  on an unidentified track, which `chapters` skips too. Keyed by **saved** track
  state, not the page's live fields, so the preview always reflects what would be
  embedded), `POST /api/done` (graceful shutdown → returns control to the CLI).
- **Host-header guard:** `_reject_foreign_host()` runs at the top of both `do_GET`
  and `do_POST` — a single gate, so a new endpoint cannot forget it. Requires the
  loopback name (`127.0.0.1`/`localhost`, case-insensitive) **and** this server's
  exact ephemeral port; anything else gets `403`. Binding loopback only stops other
  *machines*, not a page the user is already viewing: a hostile site can point its
  own name at 127.0.0.1 (DNS rebinding), after which the browser treats this server
  as same-origin and lets the page read `/api/tracklist` and `/api/audio` (the source
  recording) and POST to `/api/save`, whose corrections apply to every future run.
  A rebound request still carries the attacker's `Host`, which is what this rejects (#26).
- **Pure helpers:** `tracklist_to_api()` and `apply_edits()` are socket-free and
  unit-tested directly. `apply_edits()` maps edits onto existing tracks by stable
  `index` and puts each one through `editor.apply_track_edit()` — the TUI's own
  correction step; an edit with **no** `index` is a track inserted via the page's per-row
  "＋ Add below" control — it's appended with its own `timestamp` and the list is
  re-sorted into chronological position (inserts aren't corrections, so none is
  recorded). The page sends existing rows by `index` (not array position, which
  inserts shift) and reloads after saving new tracks so re-save can't duplicate.
  Opened with `identify --web-edit` / `-w`; mutually exclusive with `--edit`.
- **Editable set description:** the page renders `Tracklist.summary` as an always-on,
  column-aligned `<textarea>` (empty shows an "add a description" placeholder). Its
  text rides along in the `POST /api/save` body; `_handle_save()` forwards it as
  `apply_edits(..., summary=...)`, which uses an `_UNSET` sentinel — an absent
  `summary` key leaves `tracklist.summary` untouched (so older clients can't wipe it),
  while a sent value is whitespace-normalized to one paragraph (blank/None clears it),
  keeping the `to_markdown()` ↔ `parse_markdown_tracklist()` round-trip lossless.
- **Track-focused player:** the scrubber spans the *current track's window*
  (its timestamp → the next track's; the last → audio duration), not the whole
  recording. On a 4-hour set the global bar was 13.6 s/px, so fine positioning
  was unavailable; a window brings that down to roughly 0.1-0.2 s/px depending
  on track length (measured ~0.143 s/px on a 3-minute track). Windows come from
  `windowFor(i)`, derived on demand so an inserted row needs no bookkeeping for
  its *window* — but the playing track's bare array index does: `addBelow()`'s
  splice and `commit()`'s re-sort can both move it, so a module-scope
  `playingTrack` reference is re-resolved to a fresh `playingIndex` via
  `tracks.indexOf()` (`reindexPlayingTrack()`) at both mutation sites. Windows
  are clamped to ≥1s so duplicate timestamps cannot divide by zero, and
  `trackIndexAt()` lets the playing track win while the position is still inside
  its own window — its backwards walk otherwise resolves a shared timestamp to
  the *later* track, which re-scoped (and scrolled) away from the earlier one on
  the first `timeupdate` tick, so it could never be selected (#37). Playback
  deliberately runs *past* a window's end and re-scopes via `trackIndexAt()` —
  hearing the transition is how a boundary gets verified — and ±15s seeks clamp
  only to `[0, duration]`, never to the window, for the same reason. The playing
  row is highlighted (`.row.playing`) and scrolled into view whenever
  `setPlayingIndex()` is called with `scroll: true` — automatic boundary
  advance and manual prev/next, *including* `prevTrack()`'s restart branch,
  which scrolls even though the index is unchanged — and left unscrolled only
  for a row's own ▶ button, since that row is already under the cursor.
  `render()` also calls `setPlayingIndex()` (with `scroll: false`) so the
  player bar's label/count/artwork stay correct after an edit or insert, not
  just the row highlight.
  Keyboard: Space, ←/→ ±15s, ↑/↓ prev/next, all suppressed while focus is in an
  input/textarea — except `#seek`, a range input that's deliberately exempted
  so the arrows still seek when the scrubber has focus.

### `setlist_maker/playback.py` - Editor audio preview
- **PlaybackController:** Drives a non-blocking `ffplay` subprocess (`play()` / `stop()` /
  `is_playing()` / `elapsed()`), reaping the child on stop. Deliberately out-of-process: an
  earlier in-process (`sounddevice`) version locked up Textual's event loop and was removed
- **playback_available():** Capability gate — `ffplay` on PATH **and** macOS. `audio_output_available()`
  is Darwin-only (playback is unsupported elsewhere; SSH is not special-cased). `PREVIEW_SECONDS = 30`
- Seek/scrub within a track is a tracked follow-up (GitHub issue #10)

Key classes:
- `Track`: Dataclass representing a single track with timestamp, artist, title, rejected status
- `Tracklist`: Collection of tracks with markdown/JSON export methods

## Code Style

- Line length: 100 (configured in pyproject.toml)
- Ruff lint rules: E, F, W, I (errors, pyflakes, warnings, isort)
- Python 3.10+ required
