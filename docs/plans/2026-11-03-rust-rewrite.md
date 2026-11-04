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
   **Result (2026-11-03, `spikes/shazam/`, 5 positions × 12s and 30s windows, each sent once
   by Python's adaptive path and once by Rust):** pass. All 10 pairs named the same track,
   with the same number and order of match offsets. Rust's implied starts are within 5.1ms of
   Python's on the first match and 14.6ms on any match; within Rust, the 12s and 30s windows
   agree within 25ms. Python re-encodes each window to MP3 before shazamio-core decodes it,
   which likely accounts for most of the gap (unverified). The 10 Rust requests all returned 200; Python's
   statuses weren't recorded. Offline, the fingerprinter is **byte-identical** to
   shazamio-core 1.1.2 on the same PCM (`cargo test`, against a fixture made by shazamio-core).
   That proves M3's exit check 1 early; M3 still has to carry the test into
   `hindsight-core`, with `Cargo.lock` pinning `chfft`. The algorithm is SongRec's, but
   current SongRec uses `realfft`, not `chfft`, and whether the bits match then is
   unverified. To match shazamio-core, decode with symphonia with gapless trimming, take i16
   and the **first channel only** (rodio's mono conversion drops the rest), resample to
   16 kHz by rodio's integer linear interpolation, then keep the middle 10s
   (`len / 2` ± 80000 samples). rodio restarts its resampler on every decoded packet; the
   spike doesn't. At 48 kHz that changes nothing; at 44.1 kHz it's untested on the live path. This set is 48 kHz,
   which is an exact 3:1 that never interpolates; a test against rodio itself covers
   44.1 kHz. The request copies shazamio's URL, body and headers, except that it uses one
   fixed User-Agent where shazamio picks one at random.
   For spike 2: an accurate seek lands on the right sample, taking 26–53ms where decoding
   from the top takes 0.9–7.2s. The first few ms after a seek differ from a continuous
   decode, which is MP3 decoder warm-up:
   - at 12941s with a 30s window, 580 samples within the first 644 (13ms), up to 9577 LSB;
   - at 9876.4s with a 12s window, 26 samples within the first 69, up to 3 LSB;
   - at 1607s and 7033s, identical.
   That's harmless here, because the excerpt starts at least 1s into the window. M3 should
   seek a frame early and discard it.
   Verdict: continue.
2. **symphonia.** Decode the 4-hour MP3: wall time, seek accuracy to an arbitrary second, and
   decoded length vs. the header duration (the decode guard). Also m4a, flac and wav.
   **Result (2026-11-03, `spikes/shazam/src/bin/decode.rs`, symphonia 0.5.5 and 0.6.1 side by
   side; ffprobe and ffmpeg figures were run by hand):** pass. Files: the 08-26 and 09-28 Keys
   MP3s (CBR 192k, 48 kHz, no LAME delay), 2025-02-27's (44.1 kHz, LAME delay 1105), a real
   TASCAM 24-bit WAV (2h04), the real 09-28 `.caf` (16-bit BE PCM), and AAC m4a, ALAC m4a and
   FLAC transcoded from the 08-26 MP3 (none exist on disk). For M3: use 0.6.1, seek one frame
   early, trust only a stated length, and decide about CAF.
   - **Use 0.6.1.** Whole-file PCM hashes equal 0.5.5's for every MP3 (including the gapless
     LAME one), FLAC, ALAC, WAV and CAF, so spike 1's fingerprint parity carries over. AAC
     differs by at most 1 LSB in 0.09% of samples. 0.6 needs the `id3v2` feature, or it can't
     open an MP3 that starts with an ID3 tag.
   - **Full decode, one thread, 4h:** MP3 9.5s (0.6) / 9.8s (0.5), FLAC 9.4s, AAC 9.1s, ALAC
     30s, CAF 3.7s; WAV 2.0s for 2h04. No decode errors or timestamp gaps.
   - **Seeking**, compared with symphonia's own continuous decode, all channels: 30 random
     positions on each Keys MP3 (12s windows on one, 30s on the other). Seeking straight to the
     frame, 40 of the 60 windows differ in their first ≤1047 frames (22ms), by up to 27832 LSB.
     Seeking **1152 frames (one MP3 frame) early and discarding them makes all 60 identical**,
     on both versions; open + seek + decoding the window takes 8–44ms. WAV, CAF, FLAC and ALAC
     windows are identical with no preroll (2–25ms). AAC windows land at shift 0 but 20–40% of
     samples differ throughout, with or without preroll, so they aren't reproducible; noise
     substitution state is the likely cause (unverified).
   - **Against ffmpeg**, which Python's windows came from (native rate, first channel): WAV
     identical. MP3 aligns exactly (shift 0 at 6 positions) but isn't bit-equal: mean 1.5–14
     LSB, 0.06–0.32% of samples off by more than 2, peaks up to 20913 (cause not
     investigated). AAC sits 1024 frames (21.3ms) later than ffmpeg's, because symphonia
     doesn't trim the priming; its `n_frames` is 1024 long too.
   - **Peaks** (M4, measured while here): 8 seeked segments on 8 threads, each with the same
     one-frame preroll, take 1.2s for the 4h MP3 against 8.3s serial, and all 723,450 bins
     match the serial pass. An earlier run without preroll differed in 5 bins, all at seams.
     Python takes ~4s, but for different work (8 kHz mono via ffmpeg).
   - **Decode guard** (`n_frames` against the decode; both versions gave the same numbers):
     | Case | Reported | Decoded | Guard |
     |---|---:|---:|---|
     | 4h MP3, whole | 14468.3 | 14468.3 | passes |
     | MP3 cut to 60% | 8596.8 | 8596.8 | **misses** |
     | MP3, full size, last 40% zeros | 14468.3 | 8615.1 | trips |
     | WAV cut to 1GB | 7452.8 | 3472.2 | trips |
     | FLAC cut to 60% | 14468.3 | 8670.6 | trips |
     | m4a (AAC, ALAC) cut to 60% | open fails: no `moov` | | refused as an error |
     | CAF cut to 60% | open fails: end of stream | | refused as an error |
     | 2019-04-23 MP3 | 19235.1 | 12012.8 | **trips on a good file** |
     | 2019-10-22 MP3 | 12156.2 | 12156.2 | passes |

     The first-frame bytes of five MP3s were checked for Xing/Info/VBRI headers. Only
     2025-02-27's has one (Info + LAME); the rest have their length estimated from the file
     size. For CBR the estimate is exact, but it shrinks with a cut file, so a cut never trips.
     ffprobe estimates the cut file at 8596.8 too, so by the same rule Python's guard would
     have missed it (not run). 2019-04-23 is good: ffmpeg also decodes 12012.8s. symphonia
     estimates 19235s and ffprobe 16303s, so Python's guard would refuse it too (computed, not
     run). M3: trust `n_frames` only when the container states a length (WAV, CAF and FLAC
     headers, MP4 `moov`, or an MP3 Xing/Info/VBRI frame, which M3 must look for itself
     because `Info` can't tell a stated length from an estimate), and otherwise skip the guard,
     as Python did when ffprobe failed. The m4a rows had `moov` at the end (ffmpeg's default);
     a faststart m4a cut short is untested. Reading an iCloud `dataless` placeholder blocked
     while macOS downloaded it, rather than returning a short file (seen once, by accident),
     so the zero tail stands in for a file that is still being written.
   - **CAF: symphonia can't open Ray's real `.caf`**, in either version. Its `chan` chunk is 32
     bytes: a 12-byte stereo layout plus 20 of padding. symphonia reads the 12 and doesn't
     skip the rest, so it misreads every chunk after. With the padding turned into a `free`
     chunk (patched APFS clone), both versions open it, report 14430.443s as ffprobe does, and
     their windows match a continuous decode. CAF isn't in the planned file associations. If
     it should be, M3 needs an upstream fix or a workaround.
   Verdict: continue, on symphonia 0.6.1 with `id3v2`.
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
2. **Decode, offline:** seeked windows (one MP3 frame of preroll) are sample-identical to a
   continuous decode; against ffmpeg they align at shift 0 and stay within spike 2's measured
   MP3 error (mean ≤14 LSB, ≤0.5% of samples off by more than 2); and the decode guard trips
   on a cut WAV/FLAC and a zero-tailed MP3 but not on a good file with no stated length.
3. **End to end, once, online:** a ground-truth run (`-o <scratch>`) scored against Ray's
   corrected 60-track `_tracklist.md` finds at least the tracks one Python adaptive run (kept
   as the baseline, scored the same way) found, with boundary errors in the same range.
   As of 2026-11-03 that `_tracklist.md` is missing from beside the MP3 and from anywhere
   searched on disk; only the MP3 is left. Find it before M3. Ray suggests
   `2026-09-28-Keys_tracklist.md` (beside `2026-09-28-Keys.mp3`) as a substitute. It has
   already been corrected, so it's scored the same way but isn't an untouched baseline.

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
