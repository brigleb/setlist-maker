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
- **identify_sample_with_retry():** Wraps `shazamio` with exponential-backoff retry for rate limits.
  **The rate-limit branch is dead code** — it tests `"429"/"too many"/"rate"` against `str(e)`, but a
  real Shazam 429 either raises nothing at all or raises `FailedDecodeJson("Failed to decode json")`,
  which matches none of them. Left in place deliberately while `call_log.py` measures a baseline;
  see that section before changing it.
- **on_error hook:** every failure here collapses to a `None` return that the pipeline cannot tell
  from audio Shazam does not know. `on_error(exc)` hands the exception out before that distinction
  is lost, so the call log can record its *type*. Mirrors the existing `on_backoff` callback.
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
- `DEFAULT_DELAY_SECONDS = 15` (between API calls). Field evidence says this is already at the
  conservative end — comparable clients ship 10s — so there is little headroom to go faster.
- **Call log:** each sample is timed, its HTTP attempts drained from a `CallRecorder`, and one
  JSONL line written. Off unless `call_log=` is passed; the CLI resolves it (`_resolve_call_log`),
  so calling `process_single_file()` directly logs nothing. See `call_log.py`.

### `setlist_maker/summary.py` - Playlist summary generation
- **generate_summary():** Shells out to the Claude CLI (`claude -p --strict-mcp-config`) from a
  throwaway temp dir to produce a one-paragraph set description. Best-effort: returns `None`
  (warns and continues) if the CLI is missing, errors, times out, or returns nothing. On by
  default; suppressed by `identify --no-summary`. The result is stored on `Tracklist.summary`,
  rendered by `to_markdown()` inside the `<!-- summary -->` fence, and recovered from it by
  `parse_markdown_tracklist()` for editor round-trips. The markdown is the summary's **only**
  persistent store — `to_json()` deliberately omits it — so a parse that loses it loses it for good.

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
- **embed_chapters_for_tracklist() / _episode_cover_image():** The episode cover comes from the
  track starred in the web editor (`Track.is_episode_cover`), falling back to the original rule —
  the first track with *real* artwork — when nothing is starred **or** the starred track's lookup
  turns up nothing. `--cover` still outranks both, being seeded before the derivation runs.
  `source_artwork()` returning `None` must keep meaning *skip*: feeding it to
  `create_chapter_image()` yields a gradient card whose truthy bytes permanently block every
  later track from supplying a real cover, a regression that already happened once.
- **fetch_artwork():** Waterfall lookup across Shazam CDN, iTunes, Deezer, MusicBrainz/Cover Art
  Archive. Short-circuits on the first source that yields bytes.
- **artwork_candidates():** The picker's counterpart — asks *every* source and returns what each
  offers, deduplicated by URL in waterfall order (#20). Each source's single-result helper
  (`search_*_artwork`) is now a thin wrapper over its `*_artwork_candidates` sibling called with
  `limit=1`, so the two paths cannot drift. Only MusicBrainz costs more per candidate: Cover Art
  Archive reveals whether a release has a front cover only by being asked, one request each.
  `deezer_artwork_candidates()` retries with a **plain term query** when Deezer's advanced
  `artist:"..." track:"..."` syntax comes back empty, which it does for a large share of real
  tracks (measured: 0 rows for Daft Punk / One More Time and Kraftwerk / Autobahn, against 48 and
  164 plain) — so Deezer had been contributing nothing to the waterfall either. The retry fires
  only when Deezer *answered* with no rows: `_deezer_search()` returns `None` for a failed request
  and `[]` for an empty one, so an unreachable Deezer still costs one timeout, not two. Existing cached
  composites are unaffected (the key doesn't change); only fresh lookups can now resolve to Deezer,
  and tracks already marked artless keep their sticky `.fallback` marker until the cache is cleared.
- **download_image():** Refuses anything but `http(s)` via `is_fetchable_url()`. `urlopen`'s default
  opener handles `file:`, `ftp:` and `data:`, and since the picker lets a user *type* a cover URL
  that is persisted to the sidecar and re-fetched by this process on every later run, an unfiltered
  one would be a local-file read primitive reachable from a page (#20).
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
- **artwork_options():** The picker's cached fan-out. Keyed with `coverart_url` **omitted** —
  which alternates exist depends on artist/title/size, while a saved URL is one particular
  *answer*, not an input — so the `.cands` entry sits beside the no-URL variant's files.
  Unlike `source_artwork()`, an empty result is **not** cached: "no alternates" far more often
  means the network was down than that the track has none, and this is an interactive surface
  where retrying costs one click rather than a whole re-run.
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
  The one exception is `Track.artwork_pinned`, set when the user picked a cover in the web
  editor's picker: that URL is not Shazam's guess about a stale identification but the user's
  answer about *this* track, so clearing it on a later typo fix would silently discard it (#20).
  Both fields are **stripped** first (the web editor already did so at its own door), which is
  what lets the markdown decide a row's shape from the value alone; stripping before the no-op
  comparison also keeps a row re-sent with stray space from counting as a correction (#44).
- **parse_markdown_tracklist():** Parses existing markdown files for editing. The set
  description is read from the `<!-- summary -->` … `<!-- /summary -->` fence `to_markdown()`
  writes, and the lines it occupies are then **excluded from every other scan** — header, date
  and tracks. Both halves matter: without the fence a description shaped like `1. **X** - Y
  (0:00)` ended the summary early, and without the exclusion that same line was *also* read as
  a real track, so reopening a set silently traded the description for a phantom track (#16).
  HTML comments because the `.md` is a published artifact and they render as nothing; a
  description line that reads exactly like a marker is escaped with a backslash on write and
  unescaped on read (an involution, so `\<!-- /summary -->` survives too). A file with **no**
  fence — anything written before this existed — falls back to the old heuristic (prose after
  `*Generated on*`, ending at the first blank or track-shaped line), which is why old
  tracklists still reopen; the next save rewrites them fenced. Nothing can disambiguate a
  legacy file, and nothing else can recover from it: the summary is not in the sidecar.
  Two hand-edit shapes the fence can't fix are **warned about** rather than fixed silently,
  since a fix for silent loss shouldn't add a quiet failure of its own: an opening marker
  with no closing one (falls back to the legacy scan rather than reading to EOF and returning
  a tracklist with no tracks), and a closing marker moved *below* the listing, which swallows
  every track — loud because the next save would write that reading back as a well-formed
  file and take the sidecar's artwork with it. Two write-side details are also load-bearing:
  `to_markdown()` normalizes carriage returns (the reader opens in text mode, where a lone CR
  *is* a line break, so an un-normalized one splits a line the writer never escaped and can
  close the fence from inside), and it leaves a blank line inside each marker so renderers
  that escape raw HTML instead of honoring it don't fold the markers into the description's
  paragraph.
- **The listing's four shapes (`TRACK_LINE_PATTERN` / `to_markdown()`):** a track may know its
  artist, its title, both, or neither, and the markdown carries all four — `**Artist** - Title`,
  `*Unknown artist* - Title`, `**Artist**`, `*Unidentified*`. Totality is the point, not tidiness:
  the `.md` is **authoritative for which tracks exist** (the sidecar is only joined onto it by
  timestamp), so a shape the writer can emit but the pattern can't match is a row *deleted* on the
  next read, which is what `**** - Title` did to every artist-less track — reachable in one move,
  since the web editor's "＋ Add below" inserts a blank row and nothing between there and the file
  objects to a title with no artist (#44). The empty side gets a **marker rather than being left
  out** because `1. Titled Only (3:00)` is indistinguishable from prose, and this parser already
  learned that lesson in #16; the markers are italic and real values are always **bold**-wrapped,
  so no user-typed value can serialize to one — an artist literally named `*Unknown artist*` is
  written `***Unknown artist***` and reads back as itself. The artist span is `(.*?)`, not `(.+?)`,
  so files already damaged by the old writer give their track back rather than staying short — but
  the **title** span must stay `(.+?)`: the line is matched unanchored, so a title span that can
  match nothing lets the time group bind to a `(m:ss)` *inside* the title, and `**A** - (1:23)
  Reprise (10:00)` reads back as an empty title at 1:23 — losing the title, missing the sidecar
  entry (joined by timestamp) and moving the chapter mark, then cementing all of it on the next
  save. The legacy `**Artist** -  ` shape needs no help from a nullable span anyway, since `\s*-\s*`
  is greedy and the caller strips what it leaves. Anchoring the pattern with `\s*$` would fix the
  related #45 (a `(m:ss)` mid-title) but is **not** a free win: it stops matching a hand-annotated
  line like `1. **A** - T (0:00) [live]`, which turns an annotation into a deleted row — this
  format's one unforgivable failure. Because the pattern also decides where a *legacy* description
  ends, every branch is anchored on a marker or `**`, never on bare text. Both `to_markdown()` and `is_unidentified` work on
  **stripped** values, so a whitespace-only field is stored as the empty field it already is —
  untreated it survived one round trip (the reader strips) and was deleted by the next, putting
  the loss a save away from the edit that caused it.
- **Audio preview:** `p` previews the selected track's 30s window via `PlaybackController`
  (see `playback.py`). `_resolve_audio_path()` locates the source audio (threaded in from the
  CLI when known, else discovered beside the markdown). Playback stops on navigation/reject/edit
  and on unmount; gated by `playback_enabled` (set once in `on_mount`). The 0.5 s
  `_tick_playback` readout poll returns early when `is_running` is false: Textual clears
  that flag, unmounts the screens, and only then stops the app's timers, so a tick in that
  window would otherwise `NoMatches` on the label during shutdown (#35).
- **Pilot tests** (`tests/test_editor_edit.py`, `test_editor_playback.py`) drive the app via
  `app.run_test()` inside `asyncio.run()` (no pytest-asyncio). A step that depends on a
  multi-hop message chain — e.g. Enter → `Input.Submitted` → `focus()` → deferred
  `set_focus` — must wait on that state with the shared `wait_until` fixture
  (`tests/conftest.py`), not assume `pilot.pause()` let it settle: `pause()` only waits for
  messages already queued plus a CPU-time idle heuristic that a *starved* process also
  satisfies, which is why the roundtrip test flaked only under load (#35).

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
  embedded), `GET /api/artwork/options?index=N` (the picker's candidate list — see
  **Artwork curation** below), `POST /api/done` (graceful shutdown → returns control
  to the CLI). Both artwork endpoints resolve `index` through the shared
  `_track_for_query()`, so they answer for exactly the same set of tracks and 404 alike.
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
  while a sent value is whitespace-normalized to one paragraph (blank/None clears it).
  That normalization used to be what kept the `to_markdown()` ↔ `parse_markdown_tracklist()`
  round-trip lossless; since #16 fenced the description the round-trip is lossless for *any*
  text, so it is now only house style — whatever the user types, including a numbered line
  that looks exactly like a track, comes back verbatim.
- **Artwork curation (#20):** clicking a row's thumbnail opens `#artwork-overlay`, which is
  no longer just an enlarger — the composite sits above an **☆ Episode cover** toggle and a
  **Choose artwork…** button that reveals a candidate grid, a paste-a-URL box and **Automatic**.
  Consequences of it becoming interactive: only a click whose `e.target` *is* the backdrop
  dismisses it; the global keydown handler returns early while it is open (its tiles are
  `BUTTON`s, and the focus guard exempts only inputs, so the arrows would seek the audio while
  the user browses covers); the ★ toggle is disabled for a rejected track, which the sidecar does
  not carry (the server refuses the star for one too, so a stale client cannot clear a valid choice
  and store nothing in its place); `#toast` needs `z-index:60` or a toast raised over it renders behind
  the backdrop; and `[hidden] { display:none !important; }` is required because the panel's
  author `display:flex` otherwise beats the UA stylesheet's `[hidden]` rule.
  It lives in the static markup **outside `#list`**, since `render()` rebuilds every row, and it
  holds the open track as an **object reference** (`artTrack`) for the same reason
  `playingTrack` exists — a re-sort or insert moves array positions underneath it.
  Tiles load candidate URLs **directly from the source**, like the player bar's thumbnail: the
  covers differ only in the picture, and compositing each one server-side to show the identical
  lower-third would add latency and nothing else. Which tile reads as in use comes from the
  server's own `current` flag (`artCurrentUrl`), *not* from comparing `artTrack.coverart_url`:
  the endpoint offers the in-use cover through `resize_cover_art_url()` at the chapter size, so
  a Shazam URL saved at 400px arrives as the 600px one and a raw string compare marks nothing.
  Live page state outranks saved state in three places, all for the same reason -- the panel is
  the surface where the two most often differ: `showArtwork()` restores an unsaved pick instead of
  requesting the (pre-pick) server composite; `loadAlternates()` adopts the server's `current`
  flag only when the track has no pick; and the *search* is run on the artist/title the page
  passes, since correcting a misidentification and fixing its cover is one workflow and the stale
  name would offer covers for the song being corrected away.
  A pick sets `coverart_url` (the artwork cache's key, so it regenerates on its own) plus a
  client-only `_art` flag, which is both why the save payload includes the URL at all — sending
  it for every row would re-pin art `apply_track_edit()` means to drop — and why the row
  previews the raw image until the save rebuilds the composite. A pasted URL is loaded into an
  `Image` first: the server falls *through* its waterfall on a URL that will not download, so a
  dead link would not error, it would quietly composite another source's art under this track.
- **Theming:** one set of selectors, two palettes. Every color on the page reads a
  `:root` custom property; `@media (prefers-color-scheme: dark)` redefines only the
  tokens, and `color-scheme: light dark` (on `:root` and as a `<meta>`) lets native
  controls — seek slider, buttons, textarea, scrollbars — follow the OS. The player bar
  and toast share `--player-*`: dark chrome on the light page, an elevated hairlined
  surface on the dark one so they still read as bars. The only scheme-free literals
  are the artwork overlay backdrop, the thumb placeholder gradients (`GRADS`), and
  white text on the accent button / those gradients. Light mode is pixel-identical
  to the pre-theming page (#34).
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

### `setlist_maker/progress.py` - Live progress panel for the identify run

- **render_panel():** The dashboard pinned under the scrolling per-sample log — a boxed
  four-line readout whose **title and border colour *are* the phase**, so the state
  registers before a word is read. A fixed right-hand rail carries position, confidence,
  elapsed and ETA in the same cells every frame. Pure function of `RunState` plus a clock,
  so it unit-tests at a fixed width with no `Live`, no pty and no timing — exactly as
  `identify.format_progress_line()` does, and for the same reason.
- **Everything that moves is derived from the clock, not a counter the pipeline ticks.**
  `Live` re-renders on its own thread, so a renderable recomputing from `time.monotonic()`
  animates the cooldown countdown *during* `await asyncio.sleep(delay_seconds)` — which is
  why that sleep in `process_single_file` needed no restructuring (measured: 11 renders in
  an untouched 1.0s sleep). `RunState.started_at` therefore reads from the **injected**
  `clock`, not from `time.monotonic` directly, or a fake clock mixes with a real start.
- **The panel's height must never change.** `Live` erases a fixed number of lines, so a
  seventh row corrupts the redraw. Two things defend that: `_one_line()` collapses
  whitespace in every user-supplied field (one newline in a Shazam title is enough), and
  every glyph is deliberately one cell wide — no ⏳/⌛, since a glyph rich measures as 2 and
  the terminal draws as 1 bends the right border on every redraw.
- **live_display():** Wraps both paths — panel, or plain stdout — behind one object, which
  is what lets the identify loop stay a single code path with no `if live:` inside it.
  `redirect_stdout` stays on (it is what puts `shazam_client`'s surviving `print()`s
  *above* the panel); `redirect_stderr` is explicitly **off**, because rich redirects
  stderr whenever *stdout* is a terminal and `identify set.mp3 2> errors.log` would
  otherwise capture nothing and dump every warning onto the terminal.
- The rail widens past `RAIL` when the recording needs it (a 10-hour file wants
  `5:00:00 / 10:00:00`): the rail exists to hold the position steady, so widen rather than
  ellipsize the one number it is there to show.
- Gated by `identify --no-panel`, and skipped automatically when stdout is not a terminal.
  Note the two questions are **separate**: a terminal gets the colorized log either way, so
  `--no-panel` drops only the dashboard, never the log's own rendering.
- Known gap: `Live` restores the cursor on normal exit and on **Ctrl-C** (verified), but a
  SIGTERM/SIGHUP kill unwinds nothing and leaves the cursor hidden until `reset`.

### `setlist_maker/call_log.py` - Per-call telemetry for an identify run

- **The problem it exists for:** the pipeline **cannot tell a rate-limited sample from a
  genuinely unidentified one**, and never could. shazamio's own constructor sets
  `ExponentialRetry(attempts=20, max_timeout=60, statuses={500,502,503,504,429})`, so a 429
  is retried *inside* one `await recognize()` — >700s of backoff before anything reaches the
  caller — and then arrives in one of three shapes, none of them detectable: a JSON error body
  returned as a plain dict (no `"track"` key → `None`), an empty body returned as `None`, or
  `FailedDecodeJson("Failed to decode json")`, a frozen literal carrying no status. That last
  one is the shape Shazam actually serves (a ~142-byte `text/html` block page), and it means
  `shazam_client.py`'s `"429" in error_str or "too many" ... or "rate" ...` test **never fires**
  — the backoff/`on_backoff`/yellow-panel path is dead code. A throttled run therefore produces
  a confident-looking tracklist with silent holes, and `progress.json` cements them, since a
  resume treats a throttle-`None` as a finished sample.
- **`CallRecorder`:** subscribes to shazamio's `http_client.trace_config.on_request_end`.
  shazamio builds that `TraceConfig` and passes it to every request but only ever subscribes
  `on_request_start`; `on_request_end` is free and is handed the live `ClientResponse`. So this
  sees **every** HTTP attempt's status, headers and attempt number — including the 429s
  shazamio retried away, which is the one fact nothing else in the stack can observe — while
  changing no behaviour and replacing no client. Verified against a loopback server:
  two absorbed 429s, a 200, and the caller still gets its normal match. `attach()` is guarded
  and returns False rather than raising: `trace_config` is shazamio's internal detail, and
  losing the log is acceptable where failing the run is not. `TestShazamioStillExposesTheTraceSeam`
  is the canary — it fails loudly on an upgrade that moves the seam instead of logging nothing
  quietly. Prefer this over the `Shazam(http_client=...)` injection seam **for observation**:
  that one is supported and sees everything too, but it means owning a mirrored `request()`
  and a retry policy, i.e. changing behaviour while trying to measure it.
- **`describe_error()`:** classifies by exception **type**, never by substring. Two independent
  reasons: the rate-limit message contains no status at all, and a production tracker that
  grepped raw output for `429` misclassified real matches whose track ids contained those
  digits (9 in 100). The status and `Retry-After` survive only on `exc.__cause__` — aiohttp's
  `ContentTypeError`, preserved because `utils.py` re-raises `from e` — and only on the
  non-JSON branch. Shazam sends no `Retry-After` in practice; the field is recorded because
  its *absence* is itself worth confirming from real runs.
- **`CallLog`:** append-only JSONL, one `run` header plus one `call` line per sample. Opens and
  closes per line rather than holding a handle for the hour a run takes, so an interrupted run
  leaves a complete readable file. Every write is best-effort in the spirit of `summary.py`: a
  fault warns **once** and disables the log for the rest of the run — telemetry that can fail a
  run is worse than no telemetry.
- **`throttled`** is the field a review scans for, so it is true if a 429 appears in the attempt
  list **or** in the error's status. Attempts alone is not enough: if the trace seam ever moves,
  `attempts` comes back empty and the chained cause is the only surviving evidence.
- One shared `setlist-maker-calls.jsonl` beside the audio (or in `--output-dir`), **not** a
  per-file name: the point is to review several ordinary runs together. On by default
  (`--no-call-log` to disable, `--call-log PATH` to relocate; the off switch outranks an
  explicit path) — a log that must be enabled per run is empty exactly when a throttling
  question comes up.
- **Deliberately changes no behaviour.** No pacing, no retry policy, no adaptive delay. Field
  evidence puts the safe rate at 4-6 requests/min and the burst threshold at ~15-20 quick ones,
  so `DEFAULT_DELAY_SECONDS = 15` is already at the conservative end of what comparable clients
  ship (SongRec defaults to 10s) — there is little headroom, and the limit is burst-sensitive
  rather than average-sensitive, which makes AIMD and token buckets the wrong shape. Fixing the
  dead backoff path or taking over shazamio's 20 hidden retries would also mean the logs
  describe a system that was never run. Measure first.

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

### The JSON sidecar's shape

`Tracklist.to_json()` returns a **bare list** and must keep doing so. The curated
episode-cover choice is conceptually a property of the *set*, but it is stored as a flag on the
one track whose art is used precisely so the top level never changes: wrapping the list in an
object does not raise for existing readers, it degrades **silently** —
`_load_tracklist_with_artwork_urls()` iterates the loaded JSON, iterating a dict yields its keys
as strings, `"timestamp" in "tracks"` is a false substring test rather than a `TypeError`, and
the loader's `except` never fires. Every track would quietly lose its `coverart_url`, re-key the
artwork cache and re-fetch. The list is also joined back to the markdown **by timestamp**, not
by position (`to_json()` drops rejected tracks, so positions do not line up), which is why a
curated choice is a per-track flag rather than an index into anything.

### Test network guard

`tests/conftest.py` has an autouse fixture that fails any test resolving a non-loopback host.
It raises `NetworkAccessBlocked`, deliberately a **`BaseException`**: every artwork source
helper wraps its request in `except Exception` and returns `None`, so an ordinary error would be
swallowed and the unpatched test would pass quietly with no artwork — the exact silence the
guard exists to end. Patch seams instead: `setlist_maker.artwork.urllib.request.urlopen` for a
source helper, or `fetch_artwork` / `artwork_candidates` **in the module that imported it**.

## Code Style

- Line length: 100 (configured in pyproject.toml)
- Ruff lint rules: E, F, W, I (errors, pyflakes, warnings, isort)
- Python 3.10+ required
