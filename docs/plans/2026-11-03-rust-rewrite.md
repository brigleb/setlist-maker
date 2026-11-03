# Rust rewrite

Branch `rust-rewrite`. The companion file `2026-11-03-rust-rewrite-inventory.md` lists what
each Python module becomes, and the on-disk formats the Rust version must keep reading.

## Goal

A native app: drop an MP3 on its icon or window and it identifies the set, with the live
timeline, then turns into the editor. Plus a small headless CLI. One Rust codebase, no Python,
no ffmpeg.

## Decisions (2026-11-03)

- **Shell: Tauri 2**, unsigned (ad-hoc) for now. Users click "Open Anyway" once, or install
  through a Homebrew tap. Notarize later if it catches on.
- **License: GPL-3.** The fingerprinter is effectively SongRec's (`shazamio-core` reuses its
  `SignatureGenerator`, field names and Hann table under an MIT label). Ray is the sole author,
  so relicensing is his call alone.
- **CLI kept, minimal:** `identify` and `chapters`, plain line output, no panels or colours.
- **Summaries optional:** a Generate button appears only when `claude` is on PATH. The
  description field and its `<!-- summary -->` fence stay either way.
- **Dropped:** the Textual TUI and ffplay preview, the lower-third overlays (and with them the
  gradient fallback card), the sequential `--sequential` pipeline, rich panels and colour help,
  the call log, pydub/ffmpeg. Details in the inventory.

## Architecture

```
Cargo.toml                 workspace
crates/core/               setlist-core: everything with no UI
  model/                   Track, Tracklist, markdown, sidecar, corrections, episode, uploads
  engine/                  identity + boundary (pure, no I/O, no clock, no async)
  audio/                   symphonia decode, window slicing, decode guard, peaks
  shazam/                  fingerprint (SongRec-derived) + HTTP client
  artwork/                 sources, candidates, source cache
  chapters/                ID3v2.3 CHAP/CTOC/APIC + episode tags
  run/                     adaptive driver: pacing, persistence, events, sample queue
crates/cli/                `setlist-maker` binary (identify, chapters, replay, serve)
app/                       Tauri app
```

- **The front end is reused as-is.** The Tauri window loads the existing page through a
  custom URI scheme (`register_asynchronous_uri_scheme_protocol`). Its handler routes the same
  paths Python's `web_editor.py` serves (`/`, `/web/*`, `/timeline.js`, `/api/*`), so the
  page's `fetch("/api/...")` calls work unchanged. Assets are embedded with `include_dir!`.
  With no socket, there is no DNS-rebinding surface and the host-header guard goes away.
- **The same router behind a loopback HTTP server** (`setlist-maker serve <set>`, with the
  host-header guard) is what the headless Playwright checks drive, since a Tauri webview can't
  be driven that way.
- **Opening a file.** Window drops arrive as `DragDropEvent::Drop { paths }`. Drops on the
  Dock icon and Finder's "Open With" arrive as `RunEvent::Opened { urls }` (macOS, with
  `bundle.fileAssociations` for mp3/m4a/wav/flac). On Windows and Linux they come in argv.
  A set that already has a `_tracklist.md` opens in the editor. Anything else starts a live run
  (today's `--watch` flow), which becomes the editor when it finishes.
- **Python stays on the branch until cutover.** It's the parity oracle; nothing is deleted
  before M7.

## Milestones

Sizes are rough, in focused days.

### M0 Spikes: prove the risky parts (3–5 days)

Each spike's exit is a measured result written into this file.

0. **WebKit, before any Rust.** On macOS a Tauri window is WKWebView (Safari's engine), and
   the page has only ever run in Chrome. Drive today's Python `--web-edit` on a copied set
   with Playwright's WebKit build. Cover the dialogs and their Tab wrap, inline edit
   commit/Escape, the keyboard map, toasts (popover), the timeline drag, and audio scrubbing.
   Then click through the same in a bare Tauri window. Note: WKWebView's Tab skips buttons by
   default. If the fixes are small, stay on Tauri. If not, the shell becomes Electron around
   `setlist-maker serve`; the Rust core doesn't change.
   **Result (2026-11-03, Playwright WebKit vs Chromium, 9 existing scripts):** keys, menus,
   ⌘K, multi-select, toasts, states, zoom and playback match. Screenshots differ only in font
   anti-aliasing (1–4% of pixels). Two real issues:
   (a) **Tab in dialogs:** WebKit's Tab skips buttons, so in the artwork dialog it cycles
   URL field → `<body>` → dialog, and focus leaves the dialog. ⌥Tab is fine. Fix:
   `dialog.js` moves focus through the dialog's focusables itself instead of only wrapping at
   the ends.
   (b) **Edge drag drops moves:** `grid.js`'s pointermove rAF closes over the *first* move's
   `x` in a frame and ignores the rest, so a quick drag commits a stale position. Chromium has
   the bug too; WebKit's 60Hz frames expose it. Fix: store the latest `x`/`alt` on `drag`.
   Not covered: clipboard Copy (the harness can't grant it in WebKit) and the real WKWebView.
   Verdict: stay on Tauri.
1. **Fingerprint and Shazam.** From Rust, fingerprint a 12s window and a 30s window of the
   ground-truth set and send one request each. Pass: the same answer as Python, and implied
   starts within 0.1s (proves the centred 10s excerpt is reproduced).
2. **symphonia.** Decode the 4-hour MP3: wall time, seek accuracy to an arbitrary second, and
   decoded length vs. the header duration (the decode guard). Also m4a, flac and wav.
3. **Tauri.** The existing page in a window through the custom scheme. Pass: `<audio>` plays
   and scrubs a 300MB MP3 through Range requests in WKWebView; a window drop and a Dock-icon
   drop both yield a path.
4. **ID3.** The `id3` crate writes v2.3 CHAP/CTOC with per-chapter APIC; ffprobe lists the
   chapters in time order. If the crate keeps insertion order, the #33 byte-permuting fix is
   not needed.

If spike 3's Range streaming fails, fall back to a loopback server inside the app (and keep the
host guard). If spike 1 fails, the plan stops here.

### M1 Model and persistence (5–7 days)

Track/Tracklist, the markdown reader and writer, the sidecar join, `save_tracklist` write
order, atomic writes, CorrectionsDB, `apply_track_edit`, episode, uploads, progress v2 (plus the
legacy list reader). Port the Python tests for each. **Golden check:** every existing set on
Ray's disk (copied) reads and re-writes byte-identically.

### M2 Engine (7–10 days)

`identity` and `boundary` as pure Rust, with the oracle (`boundary_oracle.py`, `misid_rate`
included) and `tests/fixtures/*.json` ported as test support. **Parity harness:** a small
Python script dumps `segments()` for every prefix of every real `_progress.json`; `setlist-maker
replay` does the same in Rust; the outputs must match. No network, no waiting.

### M3 Recognition and the CLI (5–7 days)

Decoding and windows, the Shazam client, the adaptive driver (15s pacing, persist after each
probe, `_events.jsonl`, first Ctrl-C finishes and a second aborts, `--budget`), and the
`identify` CLI. Now that the HTTP client is ours, a 429 is detected and retried with backoff
instead of becoming a silent "unidentified". A throttled probe is not persisted, so a resume
asks again. Exit: a ground-truth run (`-o <scratch>`) matches Python's tracklist.

### M4 Artwork, chapters, peaks (4–6 days)

Artwork sources (Shazam CDN, iTunes, Deezer with the plain-query retry, MusicBrainz/CAA), the
candidate list, `is_fetchable_url`, a source-only cache. The chapter image is the cover,
centre-cropped, resized and JPEG-compressed, or nothing. Episode cover precedence. The
`chapters` CLI. Peaks in the `SMPK` format.

### M5 The app (7–10 days)

The Tauri shell, the router (every endpoint in the inventory), live mode, click-to-sample,
saves with revisions. Front-end changes:
- a drop screen when no set is open
- Done returns to the drop screen instead of a "closed" page
- the artwork panel previews the plain cover
- Generate in the Set tab when `/api/summary` reports `claude` is available

Verify with the Playwright runner against `serve`, on a copied set, in **both** Chromium
and WebKit. Then click through the real app, since Playwright's WebKit is close to WKWebView
but not identical.

### M6 Packaging (2–3 days)

`cargo tauri build` for a universal macOS `.app` and `.dmg`, ad-hoc signed. A Homebrew tap
with a cask for the app and a formula for the CLI. macOS first; Windows and Linux when someone
tests them.

### M7 Cutover (2–3 days)

Delete `setlist_maker/`, `tests/`, `pyproject.toml`, `uv.lock`. Move the web assets to `web/`
and run their Node tests from `cargo test`. GPL-3 `LICENSE`, a new README, and CLAUDE.md
rewritten for Rust (its hard-won notes carried into module docs). Retire `PLAN.md` and
`docs/superpowers/` into `docs/archive/`.

## Risks

- **Shazam changes or blocks the endpoint.** Same risk as today, but there's no shazamio
  upstream to fix it now. SongRec is the reference to watch.
- **Engine subtlety.** 893 lines of measured rules. The parity harness is the guard; don't
  "improve" a rule during the port.
- **Formats.** Ray's existing sets must open unchanged; M1's golden check is the guard.
- **Unsigned app.** Friction for non-technical users until notarized.

## Open questions

- Keep `--watch`-style live mode for the CLI, or live only in the app?
- Cache location: keep `~/.cache/setlist-maker` or move to `~/Library/Caches` on macOS? (The
  config dir must stay `~/.config/setlist-maker` so `corrections.json` carries over.)
