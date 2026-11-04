# Rust rewrite: inventory

Companion to `2026-11-03-rust-rewrite.md`. What happens to each Python module, and the
contracts Hindsight must keep, and the behaviour settled in the 2026-11-03 grilling.

## Python modules

| Module | Lines | Fate |
|---|---:|---|
| `boundary.py` | 893 | **Port verbatim** to `engine::boundary`. Pure. Parity-tested. |
| `identity.py` | 66 | **Port** to `engine::identity`. |
| `adaptive.py` | 447 | **Port** to `run::`. Resume from a legacy list too (it is the same replay); the first save writes v2. |
| `tracklist.py` | 713 | **Port** to `model::`. Every markdown rule carries over, see below. |
| `fileio.py` | 66 | **Port.** Atomic write that keeps the mode and symlinks; XDG config dir. |
| `episode.py` | 109 | **Port.** |
| `uploads.py` | 143 | **Port.** Pillow decode/crop becomes the `image` crate. HEIC still refused. |
| `audio.py` | 167 | **Rewrite** on symphonia, keeping the decode-completeness guard. |
| `peaks.py` | 140 | **Rewrite** on symphonia (no ffmpeg pipe); same `SMPK` output. |
| `sampler.py` | 169 | **Port** to `run::sampler` (centred window, lock, 15s spacing, live queue). |
| `shazam_client.py` | 143 | **Rewrite.** Own HTTP client, real 429 handling. Drop the dead substring test. Keep `estimate_confidence`. |
| `artwork.py` | 716 | **Port** the sources and candidates. **Drop** `create_chapter_image`'s overlay and the gradient card. |
| `artwork_cache.py` | 306 | **Shrink** to a source-only cache: no composites, so no composite keys. |
| `chapters.py` | 391 | **Port** the embed and episode-cover precedence, on the `id3` crate. **Drop** the #33 frame reorder: the crate keeps insertion order (spike 4). |
| `web_editor.py` | 1224 | **Port** the router and the save/revision logic. **Drop** the host guard in the app (kept in `serve`). |
| `identify.py` | 522 | **Keep** `finalize_outputs` and `results_to_tracklist`. **Drop** the sequential scan, dedup and smoothing pipeline. |
| `cli.py` | 884 | **Rewrite** small with clap: `identify`, `chapters`, plus hidden dev `replay` / `serve`. Flags below. |
| `summary.py` | 67 | **Keep, optional**: find `claude` (see below), run on demand from the Set tab or `identify --summary`. |
| `editor.py` | 496 | **Drop** (Textual TUI). |
| `playback.py` | 112 | **Drop** (ffplay). |
| `progress.py` | 731 | **Drop** (rich panels). The CLI prints one plain line per probe. |
| `help_format.py` | 142 | **Drop** (colour help). |
| `call_log.py` | 204 | **Drop.** It measured 0 throttled calls in 292; owning the HTTP client now makes 429s visible directly. |

Tests: the ones for dropped modules go (`test_editor*`, `test_playback`, `test_progress`,
`test_adaptive_panel`, `test_help_format`, `test_call_log`, the sequential parts of
`test_identify`). The rest become Rust tests. `test_web_timeline.py` becomes a `cargo test`
that runs Node, skipped when `node` is missing.

## File formats that must keep working

Ray's existing sets open unchanged. M1's golden check proves it.

- **`<set>_tracklist.md`** is authoritative for which tracks exist.
  - Four line shapes: `**Artist** - Title`, `*Unknown artist* - Title`, `**Artist**`,
    `*Unidentified*`.
  - Artist span nullable, title span not; unanchored match. #44 and #45 explain why.
  - The summary fence, with marker escaping and CR normalisation, plus the no-fence legacy
    fallback and its two warnings (#16).
- **`<set>_tracklist.json`** sidecar: a **bare list**, joined to the `.md` by timestamp.
  - Carries `coverart_url`, `artwork_pinned`, `is_episode_cover`.
  - Written before the `.md` (#30). A bad sidecar degrades to the markdown alone.
- **`<set>_progress.json`**: v2 `{version, audio_duration, probes}` read and written, plus two
  new keys Python's reader ignores (checked: it reads keys by name): `ended`
  (`converged` | `finished_early` | `budget` | `throttled`) and the sha256 of the tracklist the
  run wrote. The legacy bare list is read, so old sets show their probes and can resume.
- `<set>_events.jsonl` (append), `<set>_episode.json`, `<set>_cover.jpg`,
  `<set>_artwork/<hash>.jpg` with `upload:<hash>.jpg` refs validated against the same regex.
- **`~/.config/hindsight/`**: `corrections.json` (a corrupt file is renamed, never
  overwritten; #64) and `config.json` (`episode_artist`). Use the XDG rule, not the `dirs`
  crate (ADR 0003). On first launch, if it doesn't exist and `~/.config/setlist-maker/` does,
  **copy** (never move) the old files, so Python keeps working. Same for `~/.cache/hindsight`
  (artwork sources, `.fallback` markers, candidates, peaks), whose sha256 key strings match
  Python's exactly.
- **MP3 tags**: ID3**v2.3** only (#17); CHAP frames in time order (#33), so they're added
  sorted by start; top-level `TIT2`/`TALB`/`TPE1`, and a blank value writes nothing. From
  spike 4: no UTF-8 inside v2.3 (frames read from a v2.4 tag are re-marked UTF-16), at most
  255 chapters (CTOC's count is one byte), and mutagen's padding rule, so a re-embed doesn't
  move the audio.

## Behaviour to carry over (each has a Python test today)

- **The probe excerpt is the centred 10s** of the window. Every offset rule in the engine
  assumes it.
- **Resume is replay**: the engine is a deterministic fold over the probe list.
- **`apply_track_edit`**: strips both fields, stamps the `original_*` fields once, clears
  `coverart_url` unless the art is pinned, and does nothing when nothing changed.
- **Saving**: serialized; revision bumps on index changes; a stale revision is a 409; a bad
  payload is a 400.
- **Artwork**:
  - A track whose source lookup finds nothing gets no APIC.
  - The negative-result marker survives across processes.
  - A missing upload (iCloud not synced yet) writes no marker.
  - Network fetches are capped at 4 at once.
- **Fetching**: http(s) only. `file:`, `data:` and `ftp:` are refused.
- **Live mode**: saving is a 409 while live; click-to-sample goes through the run's queue;
  `finish()` clears `live` last.
- **Uploads**: an `image/*` content type only; an oversized body is drained before the 413.

## Settled behaviour (grilling, 2026-11-03)

- **CLI flags.** `hindsight identify`: `-o/--output-dir`, `--budget`, `--allow-partial`,
  `--summary` (opt-in), `--fresh` (ignore the existing tracklist and progress). `hindsight
  chapters`: `--audio`, `--cover`, `--no-artwork`. Everything else is a constant (delay 15s,
  precision, stride, refine window, identity thresholds) or gone (`--sequential`, the dedup
  flags, `--edit`/`--web-edit`/`--watch`, `--no-panel`, the call-log flags, `--no-learn`,
  `identify --chapters`). No live mode in the CLI.
- **Throttling.** Exponential backoff for about 5 minutes in total. Then the app pauses ("Shazam
  is throttling", Resume) and the run stays live; the CLI finishes early, writes the tracklist,
  records `ended: "throttled"`, names the gap and exits non-zero. Re-running resumes only the
  missing samples. A throttled answer never looks like "unidentified".
- **What a drop opens.**
  - Tracklist present: the editor. If the progress file's `ended` isn't `converged`, the editor
    offers **Continue identifying**, warning first if the tracklist's hash no longer matches
    (continuing replaces edits made since).
  - No tracklist, v2 or legacy progress: resume as a live run without asking.
  - Nothing beside it: a new live run.
  - A dropped `_tracklist.md`: opens its set.
  - Audio missing or unreadable: the editor without audio or waveform; the decode guard refuses
    a half-synced file when a run starts.
- **Stopping and closing.** One set, one window. **Finish now** in the live toolbar does what the
  first Ctrl-C does. Closing or quitting mid-run asks Finish now / Keep running / Stop; Stop keeps
  the progress file, so dropping the file again resumes. A second drop asks first: Done's Save &
  close / Discard / Cancel when editing, the same three as closing during a run.
- **Finding `claude`.** A Finder-launched app gets only `/usr/bin:/bin:/usr/sbin:/sbin`. Resolve
  once at startup, off the main path, with `$SHELL -lc 'command -v claude'`; fall back to
  `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.claude/local`. Call it as today:
  `-p --strict-mcp-config`, from a temp dir, best effort. The CLI uses its own PATH.

## Front end (`setlist_maker/web/`, ~5.2k lines incl. CSS/HTML)

Unchanged apart from:
- a drop screen, shown when no set is open
- Done returning to the drop screen
- the artwork panel showing the plain cover
- a Generate button for summaries
- Finish now, Continue identifying, and a throttled/paused state
- the dialog Tab and drag fixes from spike 0
- the episode and artwork drop zones fed from `tauri://drag-drop` by position, since the
  native drop hides HTML5 drops from the page (spike 3)

Drop events come through `window.__TAURI__` (`withGlobalTauri`), so there's still no npm and no
build step. Vendored Preact/htm stay; `web/LICENSES.md` gains the GPL note.
