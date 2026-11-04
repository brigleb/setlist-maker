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
   early, and trust only a stated length.
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
   - **Against ffmpeg**, which Python's windows came from (native rate, first channel, with
     ffmpeg started 2s early because its own `-ss` has the same warm-up): WAV identical. MP3
     aligns exactly (shift 0 at 6 positions) and differs by **at most 2 LSB**, mean 0.5, which
     looks like float-to-integer rounding. AAC sits 1024 frames (21.3ms) later than ffmpeg's,
     because symphonia doesn't trim the priming; its `n_frames` is 1024 long too.
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
     their windows match a continuous decode. Ray only uses MP3s like the ones in that
     folder (2026-11-03), so CAF stays unsupported and M3 does nothing about it.
   Verdict: continue, on symphonia 0.6.1 with `id3v2`.
3. **Tauri.** The existing page in a window through the custom scheme. Pass: `<audio>` plays
   and scrubs a 300MB MP3 through Range requests in WKWebView; a window drop and a Dock-icon
   drop both yield a path.
   **Result (2026-11-03, `spikes/tauri/`, Tauri 2.12.5 / tauri-cli 2.12.4, the 09-28 set's
   352MB MP3; the page's `/api/tracklist`, `/probes` and `/peaks` were captured from Python's
   server and served as files):** pass. The handler is `register_asynchronous_uri_scheme_protocol`
   with a thread per request. An autotest inside the page did the playing and seeking; Ray did
   the drags by hand.
   - **The page runs unchanged under `hindsight://localhost/`:** ES modules, the import map,
     62 rows, 340 sample pins, the waveform. `localStorage` works and `window.__TAURI__` is
     there. Listening for events from the page only
     needs a capability granting `core:event:default`.
   - **Range:** WebKit sends `bytes=0-1`, then `0-7`, then only closed ranges, mostly 64KB
     and at most 145KB. Over 3 runs that was 992 requests, none open-ended and none without
     a Range header, each answered in ≤0.6ms. So the response being a whole `Vec` costs
     nothing. The handler caps a ranged response at 4MB in case; a request with no Range
     would still get the whole file in one `Vec`, which M5 should refuse or cap.
   - **Playback**, in the same webview, `hindsight://` against Python's http server
     (`SPIKE_URL`), 4 runs each (the first with 9 seeks, the rest 18; the autotest grew
     between runs, and only the last run used the committed script). Seeking sets
     `currentTime`, which is what the page's scrubber does (`player.js`):
     | | `hindsight://` | http (Python) |
     |---|---|---|
     | `play()` to playing | 115–162ms | 411–573ms |
     | seek to `seeked`, median per run | 119–123ms | 121–155ms |
     | seek to `seeked`, slowest | 164ms (673ms in run 1's first 3 seeks) | 234ms |
     | 20 positions set in a row, each seek (last run) | 56–250ms | 59–239ms |

     The "20 positions" were meant 50ms apart, but timer throttling (below) spaced the last
     ten 1s apart, so WebKit made 10–11 seeks rather than a fast drag; a real drag on the
     `#seek` slider wasn't tried. Every seek landed within 7ms of its target, and playback
     moved on after it. The app uses 105–138MB of memory (its own process; WebKit's helpers
     aren't counted).
   - **The window drop** reaches Rust (`WindowEvent::DragDrop(Drop { paths, position })`) and
     the page (`tauri://drag-drop` through `window.__TAURI__`) ~1ms apart, with the real path.
     The dropped 4h MP3 then loaded in 103ms. The position is in NSView points, even though
     its type says `PhysicalPosition`. That's from wry's source, and it fits where Ray dropped.
   - **The page's own drop zones are dead in the app.** Tauri's drop handler always claims
     the drop (wry's `perform_drag_operation` never passes it on to WebKit), and across Ray's
     3 window drags (an MP3, then a JPG twice) the page saw no `dragenter` or `drop` at all
     (`dragover` wasn't logged). So the episode section and the artwork dialog never light up
     or take an image (Ray: "no dashed-line box"; the outline only shows during `dragover`).
     Turning `dragDropEnabled` off should bring them back, but a page's `File` carries no
     path, so an MP3 would have to be re-uploaded (both unverified). M5 keeps the native drop
     instead: the page maps `position` to an element
     (`elementFromPoint`), an image over a drop zone becomes an upload read from its path,
     and anything else opens a set.
   - **Dock icon and `open -a`** both arrive as `RunEvent::Opened { urls }`. Ray dragged a
     4h MP3 onto the Dock icon, and it loaded in 19ms. The URLs are percent-encoded (the 🎧
     folder), and `to_file_path()` decodes them. On a cold `open -a` the event comes ~100ms
     after start, *before* the page has loaded (~250–300ms), so M5 must hold the path until
     the page asks for it. The path never appears in argv on macOS. Finder's "Open With" sends
     the same Apple event but wasn't tried (unverified).
   - **Background throttling:** in a window that isn't in front, WebKit slows page timers to
     once a second after about 10 ticks, in both modes. Media is unaffected. That's fine for
     the live poll (3s), but it's why the spike measured seeks from media events.
   - `cargo tauri build --bundles app` makes a 9.7MB bundle. It carries only the linker's
     ad-hoc signature on the binary (identifier `tauri_spike-…`), not a bundle signature, so
     M6 should set `bundle.macOS.signingIdentity: "-"` (not tried). Launching it registered it
     with LaunchServices as an MP3 Viewer. `lsregister -u` it afterwards (done here), or it
     stays in Open With.
   - `<input type=file>` works: Ray's "Upload cover…" reached the handler as
     `POST /api/upload` (a 404 here, since the spike has no upload route).

   Verdict: continue on Tauri with the custom scheme; no loopback fallback needed.
4. **ID3.** The `id3` crate writes v2.3 CHAP/CTOC with per-chapter APIC; ffprobe lists the
   chapters in time order. If the crate keeps insertion order, the #33 byte-permuting fix is
   not needed.
   **Result (2026-11-03, `spikes/id3/`, id3 1.17.2, clones of the 09-28 set's 352MB MP3):**
   pass, and the #33 fix is not needed. `dump_fixture.py` pulled out the 62 chapters, their
   62 images (33–145KB) and the cover that Python had already embedded. Python's
   `embed_chapters` and the spike then each embedded them again into a clone.
   - **Order:** the crate writes frames in the order they were added (`add_frame` pushes onto
     a `Vec`; a test that adds them shuffled finds them shuffled on disk). Added in time order,
     the 62 CHAPs sit in time order despite art of every size, which is what scattered them
     under mutagen. M4 adds them sorted by start and doesn't port
     `_order_chap_frames_chronologically`.
   - **The same chapters as Python:** ffprobe's `-show_chapters` JSON is byte-identical to
     that of Python's file and of the original, including two Turkish titles outside Latin-1.
     Every CHAP holds TIT2 then APIC, with the same image bytes and description, and offsets
     of `0xFFFFFFFF`. CTOC is top-level and ordered, with the 62 children in order. The header
     is `ID3 03 00 00`, mutagen loads it as v2.3 with 62 CHAP and one CTOC, and the audio after
     the tag hashes the same.
   - **Where the bytes differ from Python's** (neither ffprobe nor AVFoundation shows it): the
     crate doesn't write the trailing UTF-16 NUL on text, which v2.3 makes optional, so each
     text frame is 2 bytes shorter. It rewrites the frames it didn't touch (Logic's `TSSE`, the
     `iTunNORM`/`iTunSMPB` comments) from Latin-1 to UTF-16, with the same text. Whether Apple's
     gapless playback still reads `iTunSMPB` in UTF-16 is unverified. The frame order differs
     too: Python's is TIT2, TPE1, TALB, TSSE, the comments, CTOC, the CHAPs, then the cover;
     the spike's is TSSE, the comments, the cover, TIT2, TALB, TPE1, the CHAPs, then CTOC
     (`set_title` and its kind remove a frame and append a new one).
   - **Two crate gaps, both needed in M4:**
     - A frame carried over from a v2.4 tag keeps its UTF-8, and the crate writes encoding
       byte 3 into the v2.3 tag (a test with ffmpeg's v2.4 tag caught a `TXXX`). The spike
       re-marks UTF-8 and UTF-16BE frames as UTF-16 before writing.
     - CTOC's child count is one byte, and the crate writes `len() as u8` unchecked, so 256
       chapters would make a table listing none. The spike refuses more than 255.
   - **A third gap, not fixed in the spike:** the crate doesn't convert a v2.4 tag's frames to
     v2.3 the way mutagen's `update_to_v23` does. Embedding into ffmpeg's v2.4 tag carried its
     `TDRC` (a v2.4-only date frame) into the v2.3 tag unchanged, and turned the title `x`
     into `x/`: v2.4 text ends in a NUL, and the crate writes every NUL as `/` in v2.3. Ray's
     recordings come from Logic as v2.3, so M4 only needs this for a file tagged elsewhere:
     convert or drop v2.4-only frames, and trim trailing NULs, before writing.
   - The room the spike pads into counts zero bytes after the declared tag, as the crate's
     `locate_id3v2` does; mutagen doesn't, so the two can differ on such a file (not seen
     here).
   - **Write cost:** `write_to_path` writes no padding, so any change in the tag's size moves
     the audio. The first run shrank the tag by Python's 347KB of padding and moved 352MB in
     place (111ms). The spike instead encodes the tag first and applies mutagen's padding rule
     through the public `Encoder::padding`: fill the old room if the tag fits and that leaves
     at most 10KB + 1% of the audio, else pad 1KB + 0.1%. With that, a re-embed over Python's
     tag leaves the audio in place: 5–11ms (Python 18–21ms). Embedding into a copy with no tag
     has to move it: 463–541ms over 3 runs (Python 109–114ms; the crate copies in 64KB steps),
     leaving exactly mutagen's 347,356 bytes of padding. Neither writer is atomic, so a crash
     mid-move breaks the MP3, as in Python today. The audio moves only when the tag outgrows
     its padding: the first Embed of a recording, or art growing by more than the padding.
   - **AVFoundation** (`avchapters.swift`) lists 0 chapters for Python's file as well as the
     spike's, so it can't judge chapters. It reads the same duration, title, album, artist and
     78KB cover from both. Apple Podcasts wasn't tried (unverified).

   Verdict: use the `id3` crate with the two fixes, the padding rule and a v2.4 conversion;
   don't port the reorder.

If spike 3's Range streaming fails, fall back to a loopback server inside the app (and keep the
host guard). If spike 1 fails, the plan stops here.

### M1 Model and persistence (5–7 days)

Track/Tracklist, the markdown reader and writer, the sidecar join, `save_tracklist` write
order, atomic writes, CorrectionsDB, `apply_track_edit`, episode, uploads, progress v2 (plus the
legacy list reader). Port the Python tests for each. **Golden check:** every existing set on
Ray's disk (copied) is read and saved by both Python and Hindsight. The `.md` must be
byte-equal to Python's output (not to the input: a legacy file comes back fenced, #16). Each
`.json` must parse to the same values; Hindsight writes raw UTF-8 where Python escapes `\uXXXX`.

**Result (2026-11-03, `scripts/golden_m1.py` with `crates/core/examples/resave.rs`):** pass.
`hindsight-core::model` has 130 tests, Python's ported plus the cases below.
- **Corpus.** Ray's disk has one set, 2026-09-28-Keys, in 14 distinct versions across old
  scratch copies. Its 3 progress files are all v2: the one legacy file (08-26's) is lost. So
  the script also generates cases from the shapes that broke the format before: 5000
  tracklists (with hand-edited sidecars and episode files, and dated and undated set names),
  100 progress
  files (half legacy lists), and 200 corrections whose lowercasing is unusual (İ, final Σ, ß).
  Tracklists, sidecars, episode files, the remembered artist, progress files and
  corrections: **0 differ**.
- **The check bites.** Making the title span nullable (#45) shows up as 210 of 2000, dropping
  Python's `true == 1` sidecar key as 83 of 2000; ASCII-only lowercasing fails the corrections
  key check.
- **Found:** serde_json's default float parser is an ulp off on some values: a real progress
  file's offset 108.76374218699999 came back as 108.763742187. `float_roundtrip` is on. M2's
  replay parity reads those floats.
- **Differences on purpose.** The reader's two warnings are returned, not printed. Invalid UTF-8
  in a sidecar degrades and in `corrections.json` is quarantined (Python crashed). Progress
  files carry `ended` and `tracklist_sha256`. A new upload gets a different hash, since the
  `image` crate's JPEG isn't Pillow's (old `upload:` refs still resolve). A sibling audio file
  is looked for `.mp3` first (Python's order changed from run to run).
- **Differences no writer can produce:** `\x1c`–`\x1f` aren't stripped, a time in non-ASCII
  digits isn't read as a time, a time past `i64` drops the row, `NaN` in a JSON file makes
  it unreadable, and a progress file whose time is a string or a bool won't load.
- **Left for later:** copying `~/.cache/setlist-maker` (M4, with the cache) and
  `tracklist_output_path` (M3, with the CLI). The config copy is `adopt_python_config`; nothing
  calls it until there's a CLI or an app to launch.

### M2 Engine (7–10 days)

`identity` and `boundary` as pure Rust, with the oracle (`boundary_oracle.py`, `misid_rate`
included) and `tests/fixtures/*.json` ported as test support. **Parity harness:** a small
Python script dumps `segments()` for every prefix of every real `_progress.json`; `hindsight
replay` does the same in Rust; the outputs must be **byte-equal**. No network, no waiting.
Exactness depends on three deliberate copies of Python: `round_ties_even` wherever Python
calls `round()`; a faithful port of `difflib.SequenceMatcher.ratio()` over chars, not bytes;
and insertion-ordered maps, so `max()` ties resolve the same way.

**Result (2026-11-03, `scripts/parity_m2.py` with the hidden `hindsight replay`):** pass.
`hindsight-core::engine` has 64 tests: Python's engine, segments and property tests ported, with
both fixtures.
- **Exact copies of Python.** `engine::py` covers `round(x, n)` (Rust's exact formatting,
  parsed back), `round()` half to even, first-wins `max`/`min`, `statistics.median`, and
  `repr(float)` for the dump. `engine::difflib` ports `SequenceMatcher.ratio()`, autojunk
  included. Python's `\b` treats a combining mark as a non-word character and the regex
  crate doesn't, so non-ASCII is mapped to `_`/`#` before the identity regexes; `ı` and `ſ`
  map to `I`/`S` instead, because Python's IGNORECASE reads them as `i`/`s` inside a tag
  ("Remıx"). The review found that one; the old binary fails 97 of 700 once those names are
  in the corpus. The oracle runs on a port of Python's Mersenne Twister, so the property
  tests also pin Python's exact numbers per seed (220 probes, p90 4.144984684561678, 43
  impostors of 89), not just bounds.
- **What's compared.** At every prefix: the events, segments and drops, `next_probe()`,
  `boundary_stats()`, the probes-remaining estimate, `max_ratio` and `max_boundary_width`,
  written as Python's `json.dumps` writes them.
- **Corpus.** The real data is one 340-probe run (09-28), a 2-probe run and the two 08-26
  fixtures. Generated on top: 2144 oracle runs (blur, jitter, offset dropout,
  misidentification, gaps, cut-ins, editor probes, stopped early or converged), 714 each of
  shuffled/subsampled runs with twins at one midpoint, hand-edit value shapes, names near the
  clustering thresholds, and legacy lists. 473,286 prefixes, **0 differ**.
- **The check bites.** Each of these mutants is caught, out of 700 generated files (measured as
  the generator grew, so the runs differ a little):
  - half-away rounding: 272
  - no fingerprint lead: 331
  - lower-middle median: 244
  - bisect right: 51
  - autojunk off: 104
  - Rust's `\b`: 24
  - a later zone winner on a tie: only 2, so that rule's coverage is thin.
  Ratio over bytes isn't observable, because normalized names are ASCII-only.
- **Differences no writer produces:** where Python would crash, Hindsight carries on.
  - A result that's a non-empty non-object is unidentified.
  - A non-string name is empty.
  - A non-number confidence or timeskew counts as 0.
  - A non-object offset is skipped.
  - The one `assert` in `add_probe` (two intervals rounding to the same key) skips the event.
  - With a clustering threshold above 1, which no command sets, equal keys can start two
    clusters, and a `boundary_predicted` can fire where Python's wouldn't.
- **Decided along the way:** `hindsight replay` lives in a new `crates/cli` (clap), whose
  other commands arrive in M3. Events and drops are typed enums that serialize to Python's
  dicts. `Probe.purpose` stays a string; a `ProbePlan` carries a `Purpose` enum.

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
   continuous decode; against ffmpeg (both warmed up) they align at shift 0 and differ by at
   most 2 LSB, as spike 2 measured; and the decode guard trips
   on a cut WAV/FLAC and a zero-tailed MP3 but not on a good file with no stated length.
3. **End to end, once, online:** a ground-truth run (`-o <scratch>`) scored against Ray's
   corrected 60-track `_tracklist.md` finds at least the tracks one Python adaptive run (kept
   as the baseline, scored the same way) found, with boundary errors in the same range.
   As of 2026-11-03 that `_tracklist.md` is missing from beside the MP3 and from anywhere
   searched on disk; only the MP3 is left. Find it before M3. Ray suggests
   `2026-09-28-Keys_tracklist.md` (beside `2026-09-28-Keys.mp3`) as a substitute. It has
   already been corrected, so it's scored the same way but isn't an untouched baseline.

**Result (2026-11-04, `hindsight identify` and `scripts/score_m3.py`):** pass, with two
differences explained below. 231 Rust tests (229 core, 2 cli); clippy and fmt clean; Python
untouched (835 pass).
1. **Fingerprint:** `signature_is_byte_identical_to_shazamio_cores` runs on a 12s fixture
   shazamio-core made (`scripts/fingerprint_fixture.py`), with `Cargo.lock` pinning chfft 0.3.4.
   This covers the generator only. On real windows the bytes can't match, because Python
   re-encodes each window to MP3 first (see the end-to-end result).
2. **Decode:** seeked windows equal a continuous decode; against ffmpeg they're within 2 LSB at
   shift 0 (skipped without ffmpeg unless `CI` is set). The guard trips on a cut WAV and FLAC
   and on a zero-tailed MP3 with an Info frame, and not on a CBR MP3 with no stated length.
   **Limit:** the guard can't catch a zero-tailed MP3 with no Xing/Info/VBRI frame, which is
   what Ray's Logic exports are. Possible follow-up: notice unread trailing bytes.
3. **End to end:** 340 probes, converged, 87 minutes. Two real 429s, each cleared by the
   first 20s retry. shazamio would have retried those inside the call, unseen; what Hindsight
   changes is a 429 that persists, which Python recorded as unidentified. The truth is 2026-09-28's corrected list (62 identified tracks), which was corrected from a
   Python run, so it leans towards Python's names and starts. Both runs are scored with no
   corrections applied:

   | | found | missed | median | p90 | max | ≤5s | ≤15s | not in the truth |
   |---|---|---|---|---|---|---|---|---|
   | Python (09-28) | 53 | 9 | 0.0s | 8.0s | 28.0s | 47 | 50 | 4 |
   | Hindsight | 53 | 9 | 0.0s | 8.0s | 90.0s | 47 | 49 | 14 |

   - **The same 9 missed**, mostly tracks Ray added by hand.
   - **The 90s outlier:** Harry Mudie's *Roman Dub*. The 30s coverage probe at 6465s answered
     Lloyd Jones's *Rome* for Hindsight and *Roman Dub* for Python. *Roman Dub* is the dub of
     *Rome*, on the same riddim.
   - **The 10 extra tracks at 0:42–0:43:** one-probe synthwave answers inside Altın Gün's
     *Kesik Çayır*. Re-asked on 2026-11-04, Python's own path got three of four of those
     windows wrong too (Scandroid, Sandrom twice). Hindsight's identical signature got a
     different answer on one of them. So Shazam is unstable on this stretch, and the re-encode
     tips 2 of 4 windows differently. Spike 1's 10 clean windows agreed; contested audio
     doesn't have to.
- **Decided along the way:**
  - A non-429 failure (network, non-JSON) also backs off. If it doesn't clear, the run ends
    early (`no_answer`) rather than recording the probe as unidentified, as Python did.
  - A throttled stop's message names the starts still rough. It doesn't name unprobed
    stretches.
  - `--summary` is opt-in.
  - A run that stopped early is carried on by the next `identify`, which refuses to do so if
    the tracklist was edited since.
- **Still untested:** rodio restarts its resampler per packet at 44.1 kHz on the live path.
  This set is 48 kHz.

### M4 Artwork, chapters, peaks (4–6 days)

Artwork sources (Shazam CDN, iTunes, Deezer with the plain-query retry, MusicBrainz/CAA), the
candidate list, `is_fetchable_url`, a source-only cache whose key strings match Python's exactly
(a test checks a few against values computed in Python). The chapter image is the cover,
centre-cropped, resized and JPEG-compressed, or nothing. Episode cover precedence. The
`chapters` CLI. Peaks in the `SMPK` format.
The embed follows spike 4: the `id3` crate with mutagen's padding rule, so a tag that fits
is written in place. When it doesn't fit, the crate would move the audio inside the original
file, and a crash or sleep partway through would leave the recording half-moved. So in that
case, write the tag and a copy of the audio to a temporary file beside the original, then
rename it over the original (about 350MB of writing, once per recording).

**Result (2026-11-04, on copies of the 09-28 set in scratch; commits 397464f, 0e7fc64,
676b673):** pass. 286 Rust tests; clippy and fmt clean; Python's 835 still pass.
- **Chapters, against Python's embed of the same tracklist.** ffprobe's `-show_chapters`
  JSON is byte-identical: 62 chapters with the same titles and times. The last chapter ends at
  14430.504 in both. Rust takes that end from summing packet lengths without decoding (0.05s),
  and it equals mutagen's `info.length` and the decoded length. Top-level tags are the same,
  in another order. The audio after the tag hashes the same in the original and in both copies.
- **The two write paths.**
  - Over Python's 6.0MB tag, Rust's 7.8MB tag didn't fit. Its plain covers are 39–185KB,
    median 122KB; Python's composites were 33–145KB. So the file was rewritten beside itself
    and renamed over, with mutagen's 1KB + 0.1% padding: 5.6s in all, including fetching all
    62 covers from an empty cache.
  - Embedding again went in place: same inode, 0.3s.
  - `--no-artwork` shrinks the tag past the padding rule's 10KB + 1%, so it rewrites too, as
    mutagen would.
- **Artwork.** Fresh lookups found art for all 62 tracks: 60 distinct covers, since two
  tracks are played twice (same names, same key). Of the 60, 56 are byte-identical to the `.src` Python cached under the same key. The other 4
  are pixel-identical, differing in 24 header bytes (the CDN's metadata). Artwork and peaks
  keys match Python's (artwork against computed values in a test; peaks on two real paths).
- **Peaks.**
  - Same header and pair count as Python's (721,526), aligned (envelope correlation 0.97 at lag
    0, 0.79 at ±1).
  - Rust's spans are a median 7% taller, 26% at p90. It keeps the file's own rate where ffmpeg
    low-passed to 8 kHz.
  - 1.3s against Python's 4.2s.
- **Decisions taken:**
  - The peaks mix is the channels' mean at the native rate, not ffmpeg's 8 kHz.
  - v2.4's `TIPL`/`TMCL` are dropped rather than merged into `IPLS`.
  - A cover that doesn't decode gives no picture (Python drew the gradient card).
  - A rewrite replaces the file (a new inode). It keeps the mode, symlinks, the creation date
    and the extended attributes (Finder tags, comment, colour label), less those Apple marks as
    tied to the old contents or never copied, by `xattr_preserve_for_intent(SAVE)`. Checked on a
    tagged copy of the 09-28 set. ACLs aren't copied.
  - The CLI now prints library warnings on stderr.
- **Found in review and fixed:**
  - Chapters go in sorted by start whatever order the `.md` lists them in.
  - A v2.4 `TXXX`/comment value had kept its trailing NUL (written as `/`).
  - An unidentified starred track hid an identified one.
- **Untested:** the cap of 4 concurrent fetches against real servers (it's tested with a slow
  fake). `options()` (the picker) and `peaks_for()` have no caller until M5. `~/.cache/hindsight`
  doesn't exist yet: the first real run makes it, copying Python's 35MB cache.

### M5 The app (7–10 days)

The Tauri shell, the router (every endpoint in the inventory), live mode, click-to-sample,
saves with revisions. Front-end changes:
- the two fixes from spike 0 (dialog Tab, drag), first
- the episode and artwork drop zones re-wired to `tauri://drag-drop` (spike 3: the native
  drop hides every HTML5 drop from the page)
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
