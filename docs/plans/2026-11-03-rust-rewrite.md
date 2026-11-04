# Rust rewrite: Hindsight

The Rust version ships as **Hindsight** (app, `hindsight` CLI), Ray's rename of the tool;
"Setlist Maker" is the Python name only. The companion file `2026-11-03-rust-rewrite-inventory.md`
lists what each Python module becomes, the on-disk formats Hindsight must keep reading, and the
behaviour settled in the 2026-11-03 grilling. Vocabulary is in `GLOSSARY.md`; the decisions a
reader might try to undo are in `docs/adr/`.

## Goal

A native app: drop an MP3 on its icon or window and it identifies the set, with the live
timeline, then turns into the editor. Plus a small headless CLI. One Rust codebase, no Python,
no ffmpeg.

## Decisions (2026-11-03)

- **Shell: Tauri 2** (ADR 0002), ad-hoc signed, not notarized. Install details under M6.
- **License: GPL-3** (ADR 0001). The fingerprinter is ported from SongRec's
  `SignatureGenerator`, credited. The centred 10s excerpt and 16 kHz mono resampling are
  shazamio-core's, and are reimplemented to match it.
- **Python is frozen.** No fixes or features, anywhere. It is the parity oracle until M7.
- **Rust lands on `main`** beside the Python, milestone by milestone. The `rust-rewrite` branch
  lasts until the first merge.
- **CLI kept, minimal:** `identify` and `chapters`, plain line output, no live mode. Flags are in
  the inventory.
- **Summaries optional:** a Generate button appears only when `claude` is found. The description
  field and its `<!-- summary -->` fence stay either way.
- **Folders:** `~/.config/hindsight` and `~/.cache/hindsight`, XDG rule on macOS too (ADR 0003).
- **Dropped:** the Textual TUI and ffplay preview, the lower-third overlays (and with them the
  gradient fallback card), the sequential `--sequential` pipeline, rich panels and colour help,
  the call log, pydub/ffmpeg. Details in the inventory.

## Architecture

```
Cargo.toml                 workspace
crates/core/               hindsight-core: everything with no UI
  model/                   Track, Tracklist, markdown, sidecar, corrections, episode, uploads
  engine/                  identity + boundary (pure, no I/O, no clock, no async)
  audio/                   symphonia decode, window slicing, decode guard, peaks
  shazam/                  fingerprint (SongRec-derived) + HTTP client
  artwork/                 sources, candidates, source cache
  chapters/                ID3v2.3 CHAP/CTOC/APIC + episode tags
  run/                     adaptive driver: pacing, persistence, events, sample queue
crates/cli/                `hindsight` binary (identify, chapters; hidden: replay, serve)
app/                       Tauri app, bundle id com.brigleb.hindsight
```

- **The front end is reused as-is.** The Tauri window loads the existing page through a
  custom URI scheme, `hindsight://` (`register_asynchronous_uri_scheme_protocol`). Its handler
  routes the same paths Python's `web_editor.py` serves (`/`, `/web/*`, `/timeline.js`,
  `/api/*`), so the page's `fetch("/api/...")` calls work unchanged. Assets are embedded with
  `include_dir!`. With no socket, there is no DNS-rebinding surface and the host-header guard
  goes away.
- **The same router behind a loopback HTTP server** (`hindsight serve <set>`, hidden, with the
  host-header guard) is what the headless Playwright checks drive, since a Tauri webview can't
  be driven that way.
- **Opening a file.** Window drops arrive as `DragDropEvent::Drop { paths }`. Drops on the
  Dock icon and Finder's "Open With" arrive as `RunEvent::Opened { urls }` (macOS, with
  `bundle.fileAssociations` for mp3/m4a/wav/flac). On Windows and Linux they come in argv.
  One set per window. What a drop does in each state, and stopping or closing mid-run, are in
  the inventory.
- **Python stays until cutover.** Nothing is deleted before M7.

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
   `hindsight serve`; the Rust core doesn't change.
   **Result (2026-11-03, Playwright WebKit vs Chromium, 9 existing scripts):** keys, menus,
   ⌘K, multi-select, toasts, states, zoom and playback match. Screenshots differ only in font
   anti-aliasing (1–4% of pixels). Two real issues, both fixed early in M5 in the shared web
   assets (not in Python):
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
Ray's disk (copied) is read and saved by both Python and Hindsight. The `.md` must be
byte-equal to Python's output (not to the input: a legacy file comes back fenced, #16). Each
`.json` must parse to the same values; Hindsight writes raw UTF-8 where Python escapes `\uXXXX`.

### M2 Engine (7–10 days)

`identity` and `boundary` as pure Rust, with the oracle (`boundary_oracle.py`, `misid_rate`
included) and `tests/fixtures/*.json` ported as test support. **Parity harness:** a small
Python script dumps `segments()` for every prefix of every real `_progress.json`; `hindsight
replay` does the same in Rust; the outputs must be **byte-equal**. No network, no waiting.
Exactness depends on three deliberate copies of Python: `round_ties_even` wherever Python
calls `round()`; a faithful port of `difflib.SequenceMatcher.ratio()` over chars, not bytes;
and insertion-ordered maps, so `max()` ties resolve the same way.

### M3 Recognition and the CLI (5–7 days)

Decoding and windows, the Shazam client, the adaptive driver (15s pacing, persist after each
probe, `_events.jsonl`, first Ctrl-C finishes and a second aborts, `--budget`), and the
`identify` CLI. Now that the HTTP client is ours, a 429 is detected and retried with backoff
instead of becoming a silent "unidentified". A throttled probe is not persisted, so a resume
asks again. Backoff gives up after about 5 minutes; what happens then is in the inventory.
Exit, three checks:
1. **Fingerprint, offline:** identical 16 kHz mono PCM gives byte-identical signatures from
   Hindsight and shazamio-core (a fixture shazamio-core produced once; runs in `cargo test`).
2. **Decode, offline:** symphonia and ffmpeg give near-identical 16 kHz mono PCM for the same
   windows, seeks within a few ms, and the decode guard agrees with ffprobe's duration.
3. **End to end, once, online:** a ground-truth run (`-o <scratch>`) scored against Ray's
   corrected 60-track `_tracklist.md` finds at least the tracks one Python adaptive run (kept
   as the baseline, scored the same way) found, with boundary errors in the same range.

### M4 Artwork, chapters, peaks (4–6 days)

Artwork sources (Shazam CDN, iTunes, Deezer with the plain-query retry, MusicBrainz/CAA), the
candidate list, `is_fetchable_url`, a source-only cache whose key strings match Python's exactly
(a test checks a few against values computed in Python). The chapter image is the cover,
centre-cropped, resized and JPEG-compressed, or nothing. Episode cover precedence. The
`chapters` CLI. Peaks in the `SMPK` format.

### M5 The app (7–10 days)

The Tauri shell, the router (every endpoint in the inventory), live mode, click-to-sample,
saves with revisions. Front-end changes:
- the two fixes from spike 0 (dialog Tab, drag), first
- a drop screen when no set is open
- Done returns to the drop screen instead of a "closed" page
- Finish now in the live toolbar; Continue identifying for a run that didn't converge;
  a paused state when Shazam is throttling
- the artwork panel previews the plain cover
- Generate in the Set tab when `/api/summary` reports `claude` is available (found through
  the login shell, see the inventory)

Verify with the Playwright runner against `serve`, on a copied set, in **both** Chromium
and WebKit. Then click through the real app, since Playwright's WebKit is close to WKWebView
but not identical.

### M6 Packaging (2–3 days)

`cargo tauri build` for a universal macOS `.app` and `.dmg`, ad-hoc signed, on GitHub
Releases. The README walks through macOS 15+'s first launch: open the app, then System
Settings → Privacy & Security → **Open Anyway**, then confirm (Control-click → Open no longer
bypasses Gatekeeper). A Homebrew tap with a formula for the CLI (formula installs are never
quarantined; needs `brew trust` since Homebrew 6.0). A cask that strips quarantine in a
postflight only if a clean-machine test shows it works (`--no-quarantine` is gone since 6.0).
No updater; once someone else uses it, a launch check for a newer GitHub release that shows a
link. Revisit Tauri's updater together with notarizing. macOS first; Windows and Linux when
someone tests them.

### M7 Cutover (2–3 days)

Delete `setlist_maker/`, `tests/`, `pyproject.toml`, `uv.lock`. Move the web assets to `web/`
and run their Node tests from `cargo test`. GPL-3 `LICENSE` crediting SongRec, a new README, and
CLAUDE.md rewritten for Rust (its hard-won notes carried into module docs). Retire `PLAN.md` and
`docs/superpowers/` into `docs/archive/`. Delete `~/.config/hindsight` and launch once, so the
corrections Python learned since the first copy come across. Rename the repo to
`brigleb/hindsight`. `uv tool uninstall setlist-maker`.

## Risks

- **Shazam changes or blocks the endpoint.** Same risk as today, but there's no shazamio
  upstream to fix it now. SongRec is the reference to watch.
- **Engine subtlety.** 893 lines of measured rules. The parity harness is the guard; don't
  "improve" a rule during the port.
- **Formats.** Ray's existing sets must open unchanged; M1's golden check is the guard.
- **Unsigned app.** Open Anyway is a Settings detour for non-technical users until notarized.
  Whether macOS 26 tightened it further is unverified.
