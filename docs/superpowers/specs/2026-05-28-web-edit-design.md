# Design: `--web-edit` browser tracklist editor

**Date:** 2026-05-28
**Status:** Approved (pending spec review)
**Topic:** A browser-based alternative to the Textual TUI editor (`--edit`), opened with `--web-edit` / `-w`.

## Summary

Setlist Maker's tracklist editor today is a terminal UI built on Textual (`setlist_maker/editor.py`, the `--edit` flag). This adds a second, **additive** front end: a clean single-page web editor served from a tiny local HTTP server and opened in the user's browser with `--web-edit` (`-w`). The TUI editor is left untouched.

The editor's model layer is already cleanly separated from the TUI, so this is a presentation-layer addition, not a rewrite. The new front end reuses `Track`, `Tracklist`, `parse_markdown_tracklist()`, `Tracklist.to_markdown()` / `to_json()`, and `CorrectionsDB` exactly as the TUI uses them, and produces byte-for-byte the same `.md` + `.json` + corrections outputs.

## Motivation

- The TUI's audio preview depends on `ffplay` and is **macOS-only** (`playback.py`). A browser front end uses HTML5 `<audio>` + HTTP Range requests, which is cross-platform and gives a real scrubber for free — covering the seek/scrub wishlist tracked in issue #10.
- A web UI is more approachable for reviewing/correcting a long set than a keyboard-driven TUI, especially with album art and a familiar player.

## Goals

- `setlist-maker recording.mp3 --web-edit` (and `-w`) opens a browser editor after identifying (or on a reused/existing tracklist).
- `setlist-maker tracklist.md --web-edit` opens the editor directly on an existing markdown tracklist.
- Feature parity with the TUI editor: reject/restore tracks, edit artist/title, preview audio, save (writing `.md` + `.json`), and learn corrections via `CorrectionsDB`.
- Chains with `--chapters` exactly as `--edit` does (control returns to the CLI when the user clicks Done).
- Zero new runtime dependencies.

## Non-goals

- Replacing or modifying the TUI editor.
- Editing the set summary text, reordering tracks, or adding/deleting tracks (neither does the TUI; out of scope for v1).
- Remote/multi-user access or authentication beyond binding to loopback.
- Live artwork fetching during editing (that remains the `chapters` flow's job).

## Decisions (from brainstorming)

1. **Look:** "Player list" — roomy rows with album-art thumbnails and a persistent bottom player + scrubber. (Chosen over a compact spreadsheet table.)
2. **Lifecycle:** Auto-open browser. **Save** writes files and keeps the session open; **Done** gracefully shuts the server down and returns control to the CLI (mirrors the TUI's S = save / Q = quit). This is what makes `--web-edit --chapters` chain.
3. **Audio preview:** Scrub the **whole recording** — ▶ on a track seeks the player to that track's start, then the user can scrub anywhere. No artificial 30s cap (less code, more useful for checking boundaries).
4. **Both flags:** Passing `--edit` and `--web-edit` together is an **error** with a clear message (ambiguous intent), validated up front.
5. **Flag name:** `--web-edit` with short alias `-w`. `-e` / `--edit` is unchanged.
6. **Dependencies:** stdlib `http.server` only; no Flask/FastAPI.

## Architecture

### New files

- **`setlist_maker/web_editor.py`** — server, request handler, browser launch, graceful shutdown, and the public entry point `run_web_editor()`.
- **`setlist_maker/web_editor.html`** — the self-contained single-page app (HTML + inline CSS + inline JS), loaded at runtime via `importlib.resources.files("setlist_maker").joinpath("web_editor.html")`. Kept as a real `.html` file (not a Python string) for readability; registered as setuptools package-data.

### Public entry point

```python
def run_web_editor(
    tracklist: Tracklist,
    output_path: Path,
    use_corrections: bool = True,
    audio_path: Path | None = None,
) -> None:
    ...
```

A drop-in sibling of `run_editor()` (same signature) so CLI wiring is symmetric. It:
1. Builds a `CorrectionsDB` (when `use_corrections`) and applies known corrections, identical to `run_editor()` (this shared preamble may be factored into a small helper reused by both).
2. Resolves the audio path (the CLI-provided `audio_path` if present and existing, else `find_audio_file(output_path)`), same logic as the TUI's `_resolve_audio_path()`.
3. Starts a `ThreadingHTTPServer` bound to `127.0.0.1` on an ephemeral port (port `0`), opens the browser at the resulting URL via `webbrowser.open()`, and serves requests until `/api/done` triggers shutdown, at which point it returns.

### Server

- `http.server.ThreadingHTTPServer` + a `BaseHTTPRequestHandler` subclass. Threading lets the audio stream and API calls overlap.
- Bound to `127.0.0.1` (loopback only) on an ephemeral port; the actual port is read back from the socket and used to build the open URL.
- The handler holds references to the live `Tracklist`, `output_path`, `corrections_db`, and resolved `audio_path` (passed via the server instance, not globals).
- Graceful shutdown: `/api/done` sets a flag and calls `server.shutdown()` from a separate thread (so the handler can finish responding first). `run_web_editor()` blocks on `serve_forever()` in the main thread and returns once shutdown completes.
- A `print()` to the terminal echoes the URL in case the browser doesn't auto-open ("Editing in your browser: http://127.0.0.1:PORT — press Ctrl-C to stop"). Ctrl-C (`KeyboardInterrupt`) is caught as an alternate clean exit.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/` | Serve `web_editor.html`. |
| `GET`  | `/api/tracklist` | Return JSON: `source_file`, `summary`, and a `tracks` array (see schema). |
| `POST` | `/api/save` | Apply edits + rejections to the in-memory `Tracklist`, record corrections, write `.md` + `.json` + corrections. Returns `{ "ok": true }` plus updated counts. |
| `GET`  | `/api/audio` | Stream the resolved audio file with **HTTP Range** support (status 206 + `Content-Range` when a `Range` header is present; full 200 otherwise). Returns 404 when no audio is resolved. |
| `POST` | `/api/done` | Graceful shutdown; control returns to the CLI. |

Unknown paths return 404. Everything is served same-origin from one loopback port, so no CORS headers are needed.

### Tracklist JSON (GET `/api/tracklist`)

Each track carries what the front end needs to render and round-trip:

```json
{
  "index": 0,
  "timestamp": 0,
  "time": "0:00",
  "artist": "Bicep",
  "title": "Glue",
  "rejected": false,
  "is_unidentified": false,
  "coverart_url": "https://is1-ssl.mzstatic.com/.../cover.jpg",
  "original_artist": "Bicep",
  "original_title": "Glue"
}
```

`index` is the stable position in `tracklist.tracks`; the save payload echoes it back so the server maps edits to the right `Track`.

### Save payload (POST `/api/save`)

The front end sends the full working list:

```json
{ "tracks": [ { "index": 0, "artist": "Bicep", "title": "Glue", "rejected": false }, ... ] }
```

The server walks `tracklist.tracks` by `index` and, for each:
- If `artist`/`title` changed, set `original_artist`/`original_title` first (if not already set), then apply the new values — mirroring `editor._on_edit_complete()` so `was_corrected` and correction-learning behave identically.
- Record corrections to `CorrectionsDB` when `use_corrections` and the track was corrected.
- Set `rejected`.

Then write markdown (`tracklist.to_markdown()`), the JSON sidecar (`tracklist.to_json()`), and `corrections_db.save()` — the exact sequence in `editor.action_save()`. This logic is shared with the TUI by extracting a small `save_tracklist(tracklist, output_path, corrections_db)` helper used by both editors, so the two front ends can't drift.

### Audio streaming (GET `/api/audio`)

Implements a minimal Range handler over the resolved audio file:
- No `Range` header → `200` with full `Content-Length` and `Accept-Ranges: bytes`.
- `Range: bytes=START-[END]` → `206 Partial Content` with `Content-Range: bytes START-END/TOTAL` and only that slice.
- Content-Type from the file suffix (`audio/mpeg` for `.mp3`, etc.; fall back to `application/octet-stream`).
- Missing/moved audio → `404`; the front end then disables the player and shows a note instead of erroring.

### UI/UX (`web_editor.html`)

Single page, system font, light theme with a dark bottom player bar (the approved mockup):
- **Header:** source filename, a counts line (`N tracks · X edited · Y rejected`), **Save** (primary) and **Done** buttons. Optional summary paragraph shown muted/italic below the header when present.
- **Track rows:** album-art thumbnail (from `coverart_url`, with a generated gradient/`?` placeholder on missing/`onerror`), artist (bold) + title (muted) stacked, timestamp, ▶ play, and ✕ reject.
  - **Inline edit:** clicking a row's text turns artist/title into inputs in place with ✓/✕ (Enter saves the field, Escape cancels). No modal.
  - **Rejected:** row dimmed + strikethrough; ✕ becomes ↩ to restore. Rejected tracks are excluded from saved output exactly as today.
  - **Unidentified:** amber "Unidentified / add a label"; clicking opens the same inline editor.
  - An **unsaved-changes** indicator appears once anything is edited/rejected; a `beforeunload` prompt guards against closing the tab with unsaved edits.
- **Player bar (persistent, bottom):** thumbnail, play/pause, current-track label, a scrubber (seek by click/drag), and `position / total` time readout. Driven by one HTML5 `<audio src="/api/audio">`; ▶ on a row sets `audio.currentTime` to the track's `timestamp` and plays.

All state lives in the page's JS; **Save** POSTs the whole list. There is no per-keystroke server traffic.

### CLI integration (`cli.py`)

- Add `-w` / `--web-edit` (`action="store_true"`) to the `identify` subparser, documented alongside `--edit` in the epilog.
- In `cmd_identify`, **before** doing work, error out if both `args.edit` and `args.web_edit` are set: print a clear message and `sys.exit(1)`.
- Wherever `cmd_identify` currently calls `run_editor(...)` (both the existing-`.md` branch and the post-identify branch), call `run_web_editor(...)` instead when `args.web_edit` is set, passing the same arguments (`use_corrections=not args.no_learn`, and `audio_path=audio_path` in the fresh-identify branch).
- `--chapters` chaining is unchanged: it already runs after the editor call returns.

### Error handling

- **No audio resolved:** `/api/audio` 404s; the page disables the player and shows "Audio not found next to the tracklist — preview unavailable." Editing/saving still work.
- **Save failure (I/O):** `/api/save` returns `500` with a message; the page surfaces a toast and keeps the unsaved state so nothing is lost.
- **Port already in use:** ephemeral port (`0`) makes collisions effectively impossible; any bind error is reported to the terminal and exits non-zero.
- **Browser doesn't open:** the URL is always printed to the terminal as a fallback.
- **Ctrl-C:** caught as a clean shutdown equivalent to Done (without writing — Save is explicit).

### Security

Loopback-only bind (`127.0.0.1`); the server is single-user and ephemeral. No auth/token in v1 (consistent with a local CLI tool). Documented as localhost-only.

## Testing (TDD)

Tests start the handler on an ephemeral port in a background thread and drive it with `urllib`/`http.client`:
- `GET /api/tracklist` returns the expected shape and values for a known `Tracklist` (including `is_unidentified`, `coverart_url`, `original_*`).
- `POST /api/save` with edits + a rejection writes markdown and JSON matching `to_markdown()` / `to_json()`, and records the expected entry in a temp `CorrectionsDB`.
- `GET /api/audio` with a `Range: bytes=0-99` header returns `206`, correct `Content-Range`, and exactly 100 bytes; without a Range returns `200` and the full file; missing audio returns `404`.
- `POST /api/done` shuts the server down and `run_web_editor()` returns.
- The shared `save_tracklist()` helper is covered directly (and the TUI continues to pass its existing tests through it).

No browser automation is required for the unit tests; the HTML page is static and exercised manually (and optionally via the Chrome extension during development).

## Housekeeping

- Add `.superpowers/` to `.gitignore`.
- Register `web_editor.html` as package data in `pyproject.toml` (`[tool.setuptools.package-data]` → `setlist_maker = ["web_editor.html"]`).
- Update `CLAUDE.md` architecture notes and the CLI help/README to document `--web-edit`.

## Rough build sequence

1. Extract shared helpers from `editor.py`: `save_tracklist()` and the corrections-preamble/audio-resolution bits, with tests still green.
2. `web_editor.py`: server + handler + `run_web_editor()`, TDD per endpoint.
3. `web_editor.html`: the single-page app.
4. CLI wiring (`-w/--web-edit`, both-flags guard, `run_web_editor` calls).
5. Packaging + `.gitignore` + docs.
6. Manual end-to-end pass in Chrome (identify → web-edit → save → done → chapters).

## Out of scope / future

- Editing the summary, reordering, add/delete tracks.
- Seek markers showing track boundaries on the scrubber.
- Auth/token for non-loopback use.
