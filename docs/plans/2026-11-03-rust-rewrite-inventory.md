# Rust rewrite: inventory

Companion to `2026-11-03-rust-rewrite.md`. What happens to each Python module, and the
contracts the Rust version must keep.

## Python modules

| Module | Lines | Fate |
|---|---:|---|
| `boundary.py` | 893 | **Port verbatim** to `engine::boundary`. Pure. Parity-tested. |
| `identity.py` | 66 | **Port** to `engine::identity`. |
| `adaptive.py` | 447 | **Port** to `run::`. Drop the legacy-to-adaptive *resume* path, keep the reader. |
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
| `chapters.py` | 391 | **Port** the embed and episode-cover precedence. The #33 frame reorder only if spike 4 needs it. |
| `web_editor.py` | 1224 | **Port** the router and the save/revision logic. **Drop** the host guard in the app (kept in `serve`). |
| `identify.py` | 522 | **Keep** `finalize_outputs` and `results_to_tracklist`. **Drop** the sequential scan, dedup and smoothing pipeline. |
| `cli.py` | 884 | **Rewrite** small with clap: `identify`, `chapters`, plus dev `replay` / `serve`. |
| `summary.py` | 67 | **Keep, optional**: detect `claude` on PATH, run on demand from the Set tab. |
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
- **`<set>_progress.json`**: v2 `{version, audio_duration, probes}` read and written. The
  legacy bare list is read, so old sets still show their probes on the timeline.
- `<set>_events.jsonl` (append), `<set>_episode.json`, `<set>_cover.jpg`,
  `<set>_artwork/<hash>.jpg` with `upload:<hash>.jpg` refs validated against the same regex.
- **`~/.config/setlist-maker/`**: `corrections.json` (a corrupt file is renamed, never
  overwritten; #64) and `config.json` (`episode_artist`). Use the XDG rule, not the `dirs`
  crate, which would pick `~/Library/Application Support` and lose Ray's corrections.
- **MP3 tags**: ID3**v2.3** only (#17); CHAP frames in time order (#33); top-level
  `TIT2`/`TALB`/`TPE1`, and a blank value writes nothing.

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

## Front end (`setlist_maker/web/`, ~5.2k lines incl. CSS/HTML)

Unchanged apart from:
- a drop screen, shown when no set is open
- Done returning to the drop screen
- the artwork panel showing the plain cover
- a Generate button for summaries

Drop events come through `window.__TAURI__` (`withGlobalTauri`), so there's still no npm and no
build step. Vendored Preact/htm stay; `web/LICENSES.md` gains the GPL note.
