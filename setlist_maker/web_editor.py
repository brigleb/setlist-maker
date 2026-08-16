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
from urllib.parse import parse_qs, urlparse

from setlist_maker.artwork import CHAPTER_IMAGE_SIZE, is_fetchable_url, resize_cover_art_url
from setlist_maker.artwork_cache import artwork_options, chapter_image
from setlist_maker.editor import (
    CorrectionsDB,
    Track,
    Tracklist,
    apply_track_edit,
    resolve_audio_path,
    save_tracklist,
)

# Host names a browser may legitimately use to reach this loopback server. The
# port must match too, so a rebinding attacker cannot forge a valid Host.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

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
                "episode_cover": t.is_episode_cover,
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


def _picked_artwork_url(edit: dict) -> str | None:
    """The artwork URL an edit pins, validated. None means "back to automatic".

    Refusing anything but http(s) here matters because this URL is persisted to
    the JSON sidecar and handed to ``urlopen`` by this process on every later
    run; its default opener would treat ``file://`` as a perfectly good source
    of "cover art".
    """
    url = (edit.get("coverart_url") or "").strip() or None
    if url is not None and not is_fetchable_url(url):
        raise ValueError(f"artwork URL must be http:// or https:// -- got {url!r}")
    return url


def apply_edits(
    tracklist: Tracklist,
    edits: list[dict],
    corrections_db: CorrectionsDB | None,
    summary: object = _UNSET,
) -> None:
    """Apply per-track edits and rejections in place, recording corrections.

    Corrections go through the shared ``editor.apply_track_edit`` -- the same
    call the TUI makes -- so both front ends learn corrections and invalidate
    stale artwork identically. Existing tracks are keyed
    by their stable ``index``; an edit with no ``index`` is a track the user
    inserted in the page, which is appended and re-sorted into chronological
    position. Inserted tracks are not Shazam corrections, so none is recorded.
    An optional ``summary`` (when omitted, the tracklist summary is left
    unchanged) replaces ``tracklist.summary``, normalized to a single
    paragraph; blank/None clears it.

    Two artwork keys are also optional, and both are absent from an edit the
    user did not make in the picker. ``coverart_url`` pins a chosen cover: it is
    applied *after* ``apply_track_edit``, which clears that field on a
    correction, so picking art and fixing a typo in one save keeps the art.
    ``episode_cover`` marks whose art becomes the episode-level cover. It is
    exclusive -- the last track a payload marks wins and every other is cleared
    -- and is refused on a rejected track, which the sidecar does not carry.
    """
    # Validate before mutating anything: a bad URL must not leave half the
    # payload applied and the rest dropped.
    for edit in edits:
        if "coverart_url" in edit:
            _picked_artwork_url(edit)

    by_index = dict(enumerate(tracklist.tracks))
    inserted: list[Track] = []
    chosen_cover: Track | None = None
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
        apply_track_edit(track, new_artist, new_title, corrections_db)
        track.rejected = bool(edit.get("rejected", track.rejected))
        if "coverart_url" in edit:
            # After apply_track_edit, never before: it clears coverart_url on a
            # correction, which would otherwise discard a pick made in the same
            # save. Supplying a URL is what pins it; clearing unpins.
            track.coverart_url = _picked_artwork_url(edit)
            track.artwork_pinned = track.coverart_url is not None
        if "episode_cover" in edit:
            # Never on a rejected track: to_json() drops those, so the star
            # could not be stored -- and accepting it would still clear the
            # previous, valid choice, leaving the set with no cover at all and
            # nothing to say so.
            starred = bool(edit["episode_cover"]) and not track.rejected
            track.is_episode_cover = starred
            if starred:
                chosen_cover = track

    if chosen_cover is not None:
        # One cover per set. Clearing here rather than trusting the page keeps
        # the invariant true for any client, and for a payload that marks two.
        for other in tracklist.tracks:
            if other is not chosen_cover:
                other.is_episode_cover = False

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

    def _reject_foreign_host(self) -> bool:
        """Send 403 and return True unless the Host header names this server.

        Binding to 127.0.0.1 stops other machines connecting; it does nothing
        about a page the user is already looking at. A hostile site can point
        its own name at 127.0.0.1 (DNS rebinding), at which point the browser
        treats this server as same-origin with that site and lets it *read*
        responses -- the tracklist, and the source recording streamed by
        /api/audio -- as well as POST to /api/save, whose corrections are
        applied to every future run.

        A rebound request still carries ``Host: attacker.example``, so
        requiring the loopback name and this server's exact port closes it.
        The ephemeral port is not itself a defense (a page can scan for it),
        but it does mean a rebinding attacker cannot guess the Host to forge.
        """
        host = self.headers.get("Host", "")
        name, sep, port = host.partition(":")
        if sep and port == str(self.server.server_address[1]):
            if name.lower() in _LOOPBACK_HOSTS:
                return False
        self.send_error(HTTPStatus.FORBIDDEN, "invalid Host header")
        return True

    def do_POST(self) -> None:
        if self._reject_foreign_host():
            return
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
            data = json.loads(raw)
            edits = data.get("tracks", [])
            apply_edits(
                ctx.tracklist,
                edits,
                ctx.corrections_db,
                summary=data.get("summary", _UNSET),
            )
            save_tracklist(ctx.tracklist, ctx.output_path, ctx.corrections_db)
        except Exception as exc:  # surface to the page; keep state intact
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        rejected = sum(1 for t in ctx.tracklist.tracks if t.rejected)
        edited = sum(1 for t in ctx.tracklist.tracks if t.was_corrected)
        self._send_json({"ok": True, "rejected": rejected, "edited": edited})

    def do_GET(self) -> None:
        if self._reject_foreign_host():
            return
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
        elif path == "/api/artwork":
            self._send_artwork()
        elif path == "/api/artwork/options":
            self._send_artwork_options()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _track_for_query(self) -> Track | None:
        """Resolve ``?index=N`` to a track, sending the 404 itself on failure.

        Shared by both artwork endpoints so they answer for exactly the same
        set of tracks: a bad or out-of-range index, and an unidentified track,
        which ``chapters`` skips too.
        """
        params = parse_qs(urlparse(self.path).query)
        try:
            index = int(params.get("index", [""])[0])
        except (TypeError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND, "bad index")
            return None

        tracks = self._ctx.tracklist.tracks
        if not 0 <= index < len(tracks):
            self.send_error(HTTPStatus.NOT_FOUND, "no such track")
            return None

        track = tracks[index]
        if track.is_unidentified:
            # chapters skips unidentified tracks, so there is nothing to preview
            self.send_error(HTTPStatus.NOT_FOUND, "track is unidentified")
            return None
        return track

    def _send_artwork_options(self) -> None:
        """Serve the alternate covers one track could use.

        Lazy on purpose. Unlike the composite endpoint this asks *every* source
        rather than stopping at the first that answers, so it costs a handful of
        third-party requests per track -- affordable when the user opened the
        picker on one track, ruinous if it ran for all sixty on load.

        The track's own URL is offered first and labelled, so the grid shows
        what is in use beside the alternatives rather than making the user
        remember it.

        Searches on the artist/title the page passes -- what the user is
        *currently* looking at -- rather than on saved state, which is the one
        place in this server where those differ deliberately. Someone who has
        just corrected a misidentification and not yet saved is exactly who
        reaches for this: searching the stale name would offer covers for the
        wrong song and then pin one. The composite endpoint does the opposite,
        and must, because it has to show what would be embedded.
        """
        track = self._track_for_query()
        if track is None:
            return

        params = parse_qs(urlparse(self.path).query)
        artist = (params.get("artist", [""])[0] or track.artist).strip()
        title = (params.get("title", [""])[0] or track.title).strip()

        candidates: list[dict] = []
        # Offered at the chapter image's size, which is the URL fetch_artwork
        # would actually request anyway. It also makes a Shazam URL (an Apple
        # CDN link, normally saved at 400px) collapse into iTunes' own tile for
        # the same cover instead of sitting beside it as a visual duplicate.
        in_use = (
            resize_cover_art_url(track.coverart_url, CHAPTER_IMAGE_SIZE)
            if track.coverart_url
            else None
        )
        if in_use:
            candidates.append({"source": "In use", "url": in_use, "label": ""})
        error = None
        try:
            candidates += [
                {"source": c.source, "url": c.url, "label": c.label}
                for c in artwork_options(artist, title)
            ]
        except Exception as exc:  # a handler that raises sends no response at all
            error = str(exc)

        seen: set[str] = set()
        unique = []
        for candidate in candidates:
            if candidate["url"] in seen:
                continue  # the in-use URL is normally also offered by its source
            seen.add(candidate["url"])
            candidate["current"] = candidate["url"] == in_use
            unique.append(candidate)
        self._send_json({"candidates": unique, "error": error})

    def _send_artwork(self) -> None:
        """Serve the chapter composite for one track, generating it on demand.

        Index-based rather than artist/title-from-the-page on purpose: the
        cache is authoritative, so the preview must reflect *saved* state --
        that is what ``chapters`` will embed.
        """
        track = self._track_for_query()
        if track is None:
            return

        data = chapter_image(
            artist=track.artist, title=track.title, coverart_url=track.coverart_url
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        # The page re-requests after a save; never serve a pre-edit composite.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # row scrolled away / page closed

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
