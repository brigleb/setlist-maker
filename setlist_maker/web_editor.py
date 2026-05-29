"""Browser-based tracklist editor served from a local HTTP server.

A presentation-layer alternative to the Textual TUI in ``editor.py``, opened
with ``--web-edit``. Reuses the ``Track`` / ``Tracklist`` model and
``CorrectionsDB`` and serves a single-page app (``web_editor.html``) plus a
small JSON + audio API bound to loopback (``127.0.0.1``).
"""

import json
import re
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

from setlist_maker.editor import (
    CorrectionsDB,
    Track,
    Tracklist,
    resolve_audio_path,
    save_tracklist,
)

_AUDIO_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    ".aiff": "audio/aiff",
}


def tracklist_to_api(tracklist: Tracklist) -> dict:
    """Shape a Tracklist into the JSON the web page consumes.

    ``index`` is the stable position in ``tracklist.tracks``; the save payload
    echoes it back so edits map to the right Track.
    """
    return {
        "source_file": tracklist.source_file,
        "summary": tracklist.summary,
        "tracks": [
            {
                "index": i,
                "timestamp": t.timestamp,
                "time": t.time_str,
                "artist": t.artist,
                "title": t.title,
                "rejected": t.rejected,
                "is_unidentified": t.is_unidentified,
                "coverart_url": t.coverart_url,
                "original_artist": t.original_artist,
                "original_title": t.original_title,
            }
            for i, t in enumerate(tracklist.tracks)
        ],
    }


_UNSET = object()  # "summary not provided" — distinct from an empty/cleared summary


def _normalize_summary(value: str | None) -> str | None:
    """Collapse whitespace runs to single spaces; empty -> None.

    Keeps the markdown round-trip lossless: parse_markdown_tracklist() joins
    contiguous prose lines with spaces, so a single-paragraph summary reloads
    byte-for-byte.
    """
    text = re.sub(r"\s+", " ", value or "").strip()
    return text or None


def apply_edits(
    tracklist: Tracklist,
    edits: list[dict],
    corrections_db: CorrectionsDB | None,
    summary: object = _UNSET,
) -> None:
    """Apply per-track edits and rejections in place, recording corrections.

    Mirrors ``editor._on_edit_complete`` + ``action_toggle_reject`` so the web
    editor learns corrections identically to the TUI. Existing tracks are keyed
    by their stable ``index``; an edit with no ``index`` is a track the user
    inserted in the page, which is appended and re-sorted into chronological
    position. Inserted tracks are not Shazam corrections, so none is recorded.
    An optional ``summary`` (when omitted, the tracklist summary is left
    unchanged) replaces ``tracklist.summary``, normalized to a single
    paragraph; blank/None clears it.
    """
    by_index = dict(enumerate(tracklist.tracks))
    inserted: list[Track] = []
    for edit in edits:
        new_artist = (edit.get("artist") or "").strip()
        new_title = (edit.get("title") or "").strip()
        if edit.get("index") is None:
            try:
                timestamp = max(0, int(edit.get("timestamp") or 0))
            except (TypeError, ValueError):
                timestamp = 0
            inserted.append(
                Track(
                    timestamp=timestamp,
                    artist=new_artist,
                    title=new_title,
                    rejected=bool(edit.get("rejected", False)),
                )
            )
            continue
        track = by_index.get(edit.get("index"))
        if track is None:
            continue
        if new_artist != track.artist or new_title != track.title:
            if track.original_artist is None:
                track.original_artist = track.artist
            if track.original_title is None:
                track.original_title = track.title
            track.artist = new_artist
            track.title = new_title
            if corrections_db and track.was_corrected:
                corrections_db.add_correction(
                    original_artist=track.original_artist or "",
                    original_title=track.original_title or "",
                    corrected_artist=new_artist,
                    corrected_title=new_title,
                )
        track.rejected = bool(edit.get("rejected", track.rejected))

    if inserted:
        tracklist.tracks.extend(inserted)
        tracklist.tracks.sort(key=lambda t: t.timestamp)  # stable: keeps load order on ties

    if summary is not _UNSET:
        tracklist.summary = _normalize_summary(summary)


def _load_page() -> str:
    """Read the single-page app from the packaged HTML asset (per request,
    so edits show up on refresh during development)."""
    return (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")


@dataclass
class EditorContext:
    """Mutable state shared with the request handler for one editing session."""

    tracklist: Tracklist
    output_path: Path
    corrections_db: CorrectionsDB | None
    audio_path: Path | None


class _Handler(BaseHTTPRequestHandler):
    """Request handler; reads session state from ``self.server.ctx``."""

    def log_message(self, *args) -> None:  # silence default stderr logging
        pass

    @property
    def _ctx(self) -> EditorContext:
        return self.server.ctx

    def _send_json(self, obj: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/save":
            self._handle_save()
        elif path == "/api/done":
            self._handle_done()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_done(self) -> None:
        self._send_json({"ok": True})
        # shut down from another thread so this response flushes first
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _handle_save(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        ctx = self._ctx
        try:
            edits = json.loads(raw).get("tracks", [])
            apply_edits(ctx.tracklist, edits, ctx.corrections_db)
            save_tracklist(ctx.tracklist, ctx.output_path, ctx.corrections_db)
        except Exception as exc:  # surface to the page; keep state intact
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        rejected = sum(1 for t in ctx.tracklist.tracks if t.rejected)
        edited = sum(1 for t in ctx.tracklist.tracks if t.was_corrected)
        self._send_json({"ok": True, "rejected": rejected, "edited": edited})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            body = _load_page().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/tracklist":
            self._send_json(tracklist_to_api(self._ctx.tracklist))
        elif path == "/api/audio":
            self._send_audio()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _send_audio(self) -> None:
        audio_path = self._ctx.audio_path
        if audio_path is None or not audio_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "audio not found")
            return
        size = audio_path.stat().st_size
        ctype = _AUDIO_CONTENT_TYPES.get(audio_path.suffix.lower(), "application/octet-stream")

        start, end, status = 0, size - 1, HTTPStatus.OK
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            lo, _, hi = rng[len("bytes=") :].partition("-")
            try:
                new_start = max(0, int(lo)) if lo.strip() else 0
                new_end = min(size - 1, int(hi)) if hi.strip() else size - 1
            except ValueError:
                new_start, new_end = 0, size - 1  # malformed: serve full file
            else:
                if new_start <= new_end:
                    start, end, status = new_start, new_end, HTTPStatus.PARTIAL_CONTENT
                else:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with open(audio_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break  # browser seeked/closed the stream; normal for media
                remaining -= len(chunk)


def create_server(ctx: EditorContext) -> ThreadingHTTPServer:
    """Build a loopback HTTP server bound to an ephemeral port for ``ctx``."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.ctx = ctx
    return httpd


def run_web_editor(
    tracklist: Tracklist,
    output_path: Path,
    use_corrections: bool = True,
    audio_path: Path | None = None,
    open_browser: bool = True,
) -> None:
    """Run the browser tracklist editor.

    Drop-in sibling of ``editor.run_editor``. Starts a loopback HTTP server,
    opens the browser, and serves until the user clicks Done (``/api/done``)
    or presses Ctrl-C, then returns so the CLI can continue (e.g. --chapters).
    """
    corrections_db = CorrectionsDB() if use_corrections else None
    if corrections_db:
        applied = corrections_db.apply_corrections(tracklist)
        if applied > 0:
            print(f"Applied {applied} learned correction(s) from previous sessions.")

    ctx = EditorContext(
        tracklist=tracklist,
        output_path=output_path,
        corrections_db=corrections_db,
        audio_path=resolve_audio_path(audio_path, output_path),
    )
    httpd = create_server(ctx)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"\nEditing in your browser: {url}\n(Press Ctrl-C here to stop.)")
    try:
        if open_browser:
            webbrowser.open(url)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
