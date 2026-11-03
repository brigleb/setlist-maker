"""Browser-based tracklist editor served from a local HTTP server.

A presentation-layer alternative to the Textual TUI in ``editor.py``, opened
with ``--web-edit``. Reuses the ``Track`` / ``Tracklist`` model and
``CorrectionsDB`` and serves a single-page app (``web_editor.html``) plus a
small JSON + audio API bound to loopback (``127.0.0.1``).
"""

import json
import math
import re
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from setlist_maker.adaptive import append_probe, live_snapshot, load_probes
from setlist_maker.artwork import CHAPTER_IMAGE_SIZE, is_fetchable_url, resize_cover_art_url
from setlist_maker.artwork_cache import artwork_options, chapter_image, source_artwork
from setlist_maker.audio import probe_duration_seconds
from setlist_maker.boundary import EngineConfig
from setlist_maker.editor import (
    CorrectionsDB,
    Track,
    Tracklist,
    apply_track_edit,
    resolve_audio_path,
    save_tracklist,
)
from setlist_maker.episode import episode_tags, save_episode_tags
from setlist_maker.peaks import peaks_for
from setlist_maker.sampler import SampleRequests, SampleRequestsClosed, ShazamSampler
from setlist_maker.uploads import (
    MAX_UPLOAD_BYTES,
    UploadError,
    artwork_dir_for,
    cover_path_for,
    is_upload_ref,
    set_episode_cover,
    store_upload,
    upload_path,
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


def probes_to_api(probes: list, duration: float | None) -> dict:
    """Shape a run's probes into the JSON the timeline draws as pins.

    The probes are evidence *about* the tracklist, never a second copy of it:
    the page joins each one to whichever track's window holds its midpoint, so
    a merge or split made in the editor moves the evidence with it rather than
    having to be reconciled against a folded engine answer. Order is preserved
    -- it is the order the engine asked for them, which is what a replay shows.
    """
    return {"duration": duration, "probes": [probe_to_api(p) for p in probes]}


def probe_to_api(p) -> dict:
    """One probe as a pin: where it listened, why, and what came back."""
    info = p.result or {}
    return {
        "t": p.t,
        "window": p.window,
        "purpose": p.purpose,
        "artist": info.get("artist"),
        "title": info.get("title"),
        "confidence": info.get("confidence"),
        "coverart_url": info.get("coverart_url"),
    }


def progress_path_for(output_path: Path, audio_path: Path | None) -> Path:
    """Where the identify run that produced ``output_path`` kept its probes.

    Both drivers write ``<audio stem>_progress.json`` beside the tracklist, so
    the tracklist's own ``<stem>_tracklist.md`` name is the surest key; the
    audio's stem covers a markdown file that was renamed.
    """
    name = output_path.name
    if name.endswith("_tracklist.md"):
        stem = name[: -len("_tracklist.md")]
    elif audio_path is not None:
        stem = audio_path.stem
    else:
        stem = output_path.stem
    return output_path.with_name(f"{stem}_progress.json")


_UNSET = object()  # "summary not provided" — distinct from an empty/cleared summary


def _normalize_summary(value: str | None) -> str | None:
    """Collapse whitespace runs to single spaces; empty -> None.

    Once the description was fenced in the markdown (#16) its line breaks became
    representable, so this is no longer what keeps the round-trip lossless --
    it is now only a house style: one paragraph, the shape ``generate_summary``
    produces and the shape a set description is asked for. Multi-paragraph
    descriptions would round-trip fine if that ever stops being the house style.
    """
    text = re.sub(r"\s+", " ", value or "").strip()
    return text or None


def _picked_artwork_url(edit: dict) -> str | None:
    """The artwork URL an edit pins, validated. None means "back to automatic".

    Refusing anything but http(s) here matters because this URL is persisted to
    the JSON sidecar and handed to ``urlopen`` by this process on every later
    run; its default opener would treat ``file://`` as a perfectly good source
    of "cover art". The one other thing accepted is an ``upload:`` reference to
    an image uploaded through this page, which is resolved as a local file
    under the set's own artwork folder and never fetched.
    """
    url = (edit.get("coverart_url") or "").strip() or None
    if url is not None and not (is_fetchable_url(url) or is_upload_ref(url)):
        raise ValueError(f"artwork URL must be http:// or https:// -- got {url!r}")
    return url


def _moved_start(edit: dict, track: Track) -> int | None:
    """The new start an indexed edit asks for, or None to leave it alone."""
    value = edit.get("timestamp")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:  # JSON allows NaN and Infinity
        return None
    seconds = int(round(value))
    return None if seconds == track.timestamp else seconds


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
    An existing track's ``timestamp`` moves its start (#55): a boundary edit,
    not a correction. A value that isn't a whole number of seconds >= 0 leaves
    the start where it was -- the insert path's "default to 0" would move an
    existing track to the top of the set on a typo. The list is re-sorted
    whenever a start moved, so the caller's order check sees any reordering.
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
    moved = False
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
        moved_to = _moved_start(edit, track)
        if moved_to is not None:
            track.timestamp = moved_to
            moved = True
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

    if inserted or moved:
        tracklist.tracks.extend(inserted)
        tracklist.tracks.sort(key=lambda t: t.timestamp)  # stable: keeps load order on ties

    if summary is not _UNSET:
        tracklist.summary = _normalize_summary(summary)


def _load_page() -> str:
    """Read the single-page app from the packaged HTML asset (per request,
    so edits show up on refresh during development)."""
    return (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")


def _load_timeline_script() -> str:
    """The timeline's pure derivations, kept out of the page so Node can test them."""
    return (files("setlist_maker") / "web_timeline.js").read_text(encoding="utf-8")


# The page's own modules and stylesheet (setlist_maker/web/). A plain name and
# a known extension, so a request can never walk out of that directory.
_WEB_ASSET = re.compile(r"^/web/([a-z_]+)\.(js|css)$")
_WEB_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}


def _load_web_asset(path: str) -> tuple[str, str] | None:
    """``(text, content type)`` for a ``/web/`` request, or None if there is no such file."""
    m = _WEB_ASSET.match(path)
    if not m:
        return None
    asset = files("setlist_maker") / "web" / f"{m.group(1)}.{m.group(2)}"
    if not asset.is_file():
        return None
    return asset.read_text(encoding="utf-8"), _WEB_TYPES[m.group(2)]


def _image_type(data: bytes) -> str:
    """The media type of fetched cover art, which is not always JPEG."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


@dataclass
class EditorContext:
    """Mutable state shared with the request handler for one editing session."""

    tracklist: Tracklist
    output_path: Path
    corrections_db: CorrectionsDB | None
    audio_path: Path | None
    # The run's saved probes, drawn as pins on the timeline. Optional: a set
    # identified before adaptive sampling existed has none, and the timeline
    # then draws the tracklist alone.
    progress_path: Path | None = None
    # Takes a second on the timeline and returns the Probe Shazam answered
    # with (see sampler.ShazamSampler). None disables click-to-sample.
    sampler: Callable[[float], object] | None = None
    # Set while `identify --watch` is still identifying: the page is read-only
    # and everything it shows is replayed from the progress file.
    live: "LiveRun | None" = None
    # The page said Done; the server is shutting down.
    closed: bool = False
    # Held by every handler that changes the tracklist or the files written
    # from it (/api/save, /api/chapters): the server is threaded, and two
    # saves interleaving in apply_edits() would each append the other's rows.
    state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    # Bumped whenever which index names which track changes -- an inserted row
    # re-sorts the list, and finishing a watched run replaces it. The page
    # sends back the revision it loaded, and a save made against an older one
    # is refused: its indices now point at other tracks. That is what makes a
    # double-submitted save safe, where serializing it alone would not be --
    # the second copy would insert its new row again, and rename whichever
    # tracks its stale indices now land on.
    revision: int = 0


# How long a click on the live timeline waits for the run to get to it. The
# run answers between its own probes, so this is generous, not expected.
LIVE_SAMPLE_TIMEOUT = 600.0

# Chapter embedding rewrites the MP3 in place; two at once would interleave.
_EMBED_LOCK = threading.Lock()


@dataclass
class LiveRun:
    """A run still identifying, as the watch page sees it.

    Everything comes from the progress file the run already writes after every
    probe (atomically, so a read never lands mid-write) replayed through the
    engine by ``adaptive.live_snapshot`` -- no channel into the running process
    beyond that file, and the ``requests`` queue it serves the user's clicks
    from. The replay is cached on the file's mtime and size, so polling costs
    one ``stat`` until the run records another probe.
    """

    source_file: str
    engine_config: EngineConfig | None = None
    requests: SampleRequests | None = None
    duration_hint: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _key: tuple | None = field(default=None, repr=False)
    _snap: tuple | None = field(default=None, repr=False)

    def snapshot(self, progress_path: Path) -> tuple:
        """``(tracklist, next_plan, probes, duration)`` as of the latest probe."""
        with self._lock:
            try:
                stat = progress_path.stat()
                key = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                key = None
            if self._snap is None or key != self._key:
                try:
                    self._snap = live_snapshot(
                        progress_path,
                        self.source_file,
                        self.engine_config,
                        duration_hint=self.duration_hint,
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    # Keep showing the last good answer; the next probe rewrites the file.
                    if self._snap is None:
                        self._snap = (Tracklist(source_file=self.source_file), None, [], None)
                self._key = key
            return self._snap


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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The page gave up waiting: the artwork picker aborts the cover
            # lookup for a track it has stepped past (#61). Not an error.
            pass

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
        if path in ("/api/save", "/api/sample", "/api/chapters") and not self._is_json():
            # A cross-site form or text/plain fetch can reach a loopback port
            # with a valid Host and no preflight; application/json cannot be
            # sent cross-origin without one, which this server never grants.
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json")
            return
        if path == "/api/save":
            self._handle_save()
        elif path == "/api/done":
            self._handle_done()
        elif path == "/api/sample":
            self._handle_sample()
        elif path == "/api/upload":
            self._handle_upload()
        elif path == "/api/chapters":
            self._handle_chapters()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _is_json(self) -> bool:
        ctype = self.headers.get("Content-Type", "")
        return ctype.split(";")[0].strip().lower() == "application/json"

    def _handle_done(self) -> None:
        self._ctx.closed = True
        self._send_json({"ok": True})
        # shut down from another thread so this response flushes first
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _handle_save(self) -> None:
        if self._ctx.live is not None:
            # The run writes the tracklist when it finishes, over whatever is
            # there -- an edit saved now would be silently thrown away.
            self._send_json(
                {"ok": False, "error": "still identifying; edits open when the run finishes"},
                HTTPStatus.CONFLICT,
            )
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        with self._ctx.state_lock:
            self._save_locked(raw)

    def _save_locked(self, raw: bytes) -> None:
        ctx = self._ctx
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            revision = data.get("revision")
            if revision is not None and revision != ctx.revision:
                self._send_json(
                    {
                        "ok": False,
                        "error": "the tracklist changed since this page loaded; reload it",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            edits = data.get("tracks", [])
            if not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits):
                raise ValueError("tracks must be a list of objects")
            cover = data.get("cover", _UNSET)
            episode = data.get("episode", _UNSET)
            if episode is not _UNSET and not isinstance(episode, dict):
                raise ValueError("episode must be an object with a title and an artist")
            if cover not in (_UNSET, None):
                # Checked before anything is applied, so a bad cover saves nothing.
                if not is_upload_ref(cover):
                    raise ValueError(f"episode cover must be an uploaded image -- got {cover!r}")
                if not upload_path(artwork_dir_for(ctx.output_path), cover).exists():
                    raise ValueError(f"uploaded image {cover} is missing")
            order = [id(t) for t in ctx.tracklist.tracks]
            apply_edits(
                ctx.tracklist,
                edits,
                ctx.corrections_db,
                summary=data.get("summary", _UNSET),
            )
            if [id(t) for t in ctx.tracklist.tracks] != order:
                ctx.revision += 1  # in memory now, whether or not the write below lands
            if cover not in (_UNSET, None):
                # One episode cover: an uploaded one replaces the starred track.
                for track in ctx.tracklist.tracks:
                    track.is_episode_cover = False
            save_tracklist(ctx.tracklist, ctx.output_path, ctx.corrections_db)
            if cover is not _UNSET:
                set_episode_cover(ctx.output_path, cover)
            if episode is not _UNSET:
                save_episode_tags(ctx.output_path, episode.get("title"), episode.get("artist"))
        except ValueError as exc:  # the payload's fault, incl. a body that isn't JSON
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:  # surface to the page; keep state intact
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        rejected = sum(1 for t in ctx.tracklist.tracks if t.rejected)
        edited = sum(1 for t in ctx.tracklist.tracks if t.was_corrected)
        # The revision, so a page whose save re-sorted the tracks (a moved
        # start) knows its indices are stale and reloads before saving again.
        self._send_json(
            {"ok": True, "rejected": rejected, "edited": edited, "revision": ctx.revision}
        )

    def do_GET(self) -> None:
        if self._reject_foreign_host():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send_text(_load_page(), "text/html; charset=utf-8")
        elif path == "/timeline.js":
            self._send_text(_load_timeline_script(), "text/javascript; charset=utf-8")
        elif path.startswith("/web/"):
            asset = _load_web_asset(path)
            if asset is None:
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self._send_text(*asset)
        elif path == "/api/tracklist":
            self._send_tracklist()
        elif path == "/api/probes":
            self._send_probes()
        elif path == "/api/audio":
            self._send_audio()
        elif path == "/api/peaks":
            self._send_peaks()
        elif path == "/api/artwork":
            self._send_artwork()
        elif path == "/api/artwork/options":
            self._send_artwork_options()
        elif path == "/api/artwork/source":
            self._send_artwork_source()
        elif path.startswith("/api/upload/"):
            self._send_upload(path[len("/api/upload/") :])
        elif path == "/api/cover":
            self._send_image_file(cover_path_for(self._ctx.output_path), "no-store")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _send_text(self, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_tracklist(self) -> None:
        tracklist, live = self._current_tracklist()
        # Under the save lock, so the tracks and their revision are one
        # snapshot: tracks from after an insert with the revision from before
        # it would let the page save stale indices.
        with self._ctx.state_lock:
            api = tracklist_to_api(tracklist)
            api["revision"] = self._ctx.revision
        api["live"] = live
        # A version for the page to cache-bust /api/cover with, or None when
        # the set has no uploaded episode cover.
        try:
            api["cover"] = cover_path_for(self._ctx.output_path).stat().st_mtime_ns // 1_000_000
        except OSError:
            api["cover"] = None
        # The title and artist Embed will write: saved for this set, else defaults.
        api["episode"] = episode_tags(self._ctx.output_path)
        audio = self._ctx.audio_path
        api["can_embed"] = bool(audio and audio.suffix.lower() == ".mp3" and audio.exists())
        self._send_json(api)

    def _current_tracklist(self) -> tuple[Tracklist, bool]:
        """The tracklist the page is looking at, and whether it is a live guess.

        While a run is live that is the replayed snapshot -- read, never stored
        on the context. A GET that assigned ``ctx.tracklist`` could land just
        after ``WatchSession.finish()`` installed the finished tracklist and
        put the live guess back over it, and that guess has no summary: the
        next Save would write the description out of the markdown, its only
        store, for good.
        """
        ctx = self._ctx
        live = ctx.live
        if live is not None and ctx.progress_path is not None:
            return live.snapshot(ctx.progress_path)[0], True
        return ctx.tracklist, False

    def _send_probes(self) -> None:
        """Serve the run's probes, re-read from disk on every request.

        Re-reading is deliberate: the file is small, and a run still in progress
        rewrites it after every probe, so a cached copy would be the wrong answer
        for exactly the page that polls. An absent or unreadable file is not an
        error -- it is a set with no evidence to draw.
        """
        path = self._ctx.progress_path
        live = self._ctx.live
        if live is not None and path is not None:
            _tracklist, plan, probes, duration = live.snapshot(path)
            api = probes_to_api(probes, duration)
            api["live"] = True
            api["next"] = (
                {"t": plan.t, "window": plan.window, "purpose": plan.purpose} if plan else None
            )
            api["can_sample"] = live.requests is not None
            self._send_json(api)
            return
        probes, duration = [], None
        if path is not None and path.exists():
            try:
                probes, duration = load_probes(path)
            except (OSError, ValueError, KeyError, TypeError):
                # Unreadable or not a shape load_probes knows; draw no pins.
                probes, duration = [], None
        api = probes_to_api(probes, duration)
        api["live"] = False
        api["can_sample"] = self._can_sample()
        self._send_json(api)

    def _audio_seconds(self) -> float | None:
        ctx = self._ctx
        if ctx.live is not None and ctx.progress_path is not None:
            duration = ctx.live.snapshot(ctx.progress_path)[3]
            if duration:
                return duration
        return probe_duration_seconds(ctx.audio_path) if ctx.audio_path else None

    def _can_sample(self) -> bool:
        ctx = self._ctx
        return (
            ctx.sampler is not None
            and ctx.progress_path is not None
            and ctx.audio_path is not None
            and ctx.audio_path.exists()
        )

    def _handle_sample(self) -> None:
        """Ask Shazam about one moment, keep the answer, and hand it back.

        The probe is appended to the run's progress file before the response,
        so a reload -- or a later `identify` resume, which replays that file --
        keeps it. Blocks for as long as the sampler's pacing and the lookup
        take; the page shows the pin as pending meanwhile.
        """
        live = self._ctx.live
        if live is not None and live.requests is None:
            self._send_json({"ok": False, "error": "sampling is unavailable"}, HTTPStatus.CONFLICT)
            return
        if live is None and not self._can_sample():
            self._send_json({"ok": False, "error": "sampling is unavailable"}, HTTPStatus.CONFLICT)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            t = float(json.loads(self.rfile.read(length) if length else b"{}")["t"])
            if not t >= 0:  # also refuses NaN
                raise ValueError(t)
        except (ValueError, KeyError, TypeError):
            self._send_json({"ok": False, "error": "t must be seconds"}, HTTPStatus.BAD_REQUEST)
            return
        # Past the end there is no audio to hear, and in a live run the probe
        # would be kept in the progress file as evidence forever.
        end = self._audio_seconds()
        if end is not None and t > end:
            self._send_json(
                {"ok": False, "error": f"t is past the end of the audio ({end:.0f}s)"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        ctx = self._ctx
        if live is not None:
            # The run takes it ahead of its own next probe, records it, and
            # saves it; nothing to append here.
            try:
                probe = live.requests.ask(t, timeout=LIVE_SAMPLE_TIMEOUT)
            except (SampleRequestsClosed, TimeoutError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            self._send_json({"ok": True, "probe": probe_to_api(probe)})
            return
        try:
            probe = ctx.sampler(t)
            append_probe(ctx.progress_path, probe, probe_duration_seconds(ctx.audio_path))
        except Exception as exc:  # a handler that raises sends no response at all
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        self._send_json({"ok": True, "probe": probe_to_api(probe)})

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

        tracks = self._current_tracklist()[0].tracks
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
            source = "Uploaded" if is_upload_ref(in_use) else "In use"
            candidates.append({"source": source, "url": in_use, "label": ""})
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
            artist=track.artist,
            title=track.title,
            coverart_url=track.coverart_url,
            uploads_dir=artwork_dir_for(self._ctx.output_path),
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

    def _send_artwork_source(self) -> None:
        """Serve one track's cover as found, without the chapter overlay (#60).

        Every thumbnail on the page (list, timeline, inspector, player bar)
        shows this, so a track has the same art everywhere. Before, only the
        list asked the server and the rest drew ``coverart_url``, which a track
        whose art came from further down the waterfall does not have: real art
        in the list, a gradient everywhere else. The composite stays where it
        is the thing being previewed, the artwork panel.

        Resolved through ``_track_for_query()`` like the composite, so both
        answer for the same tracks, and keyed by saved state the same way. The
        one extra 404 is a track with no art anywhere, which the page draws as
        its gradient; the composite renders that gradient server-side instead.
        """
        track = self._track_for_query()
        if track is None:
            return
        data = source_artwork(
            artist=track.artist,
            title=track.title,
            coverart_url=track.coverart_url,
            uploads_dir=artwork_dir_for(self._ctx.output_path),
        )
        if not data:
            self.send_error(HTTPStatus.NOT_FOUND, "no artwork found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", _image_type(data))
        self.send_header("Content-Length", str(len(data)))
        # The page keeps what it fetched, per save; see artwork.js artFor().
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_upload(self) -> None:
        """Store an uploaded image beside the tracklist; answer with its reference.

        The body is the raw file with its own ``image/*`` type -- never
        ``multipart/form-data``. That, like ``text/plain``, is a type a
        cross-site form may POST to a loopback port without a preflight, and
        this endpoint writes files; an ``image/*`` body cannot be sent
        cross-origin without one, which this server never grants.

        Storing is not choosing: the page pins the reference with an ordinary
        edit, so nothing about the tracklist changes until Save.
        """
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected an image/* body")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json({"ok": False, "error": "empty upload"}, HTTPStatus.BAD_REQUEST)
            return
        if length > MAX_UPLOAD_BYTES:
            # Drained in chunks and discarded, so an oversized body never lands
            # in memory -- but is read, or the client sees a reset connection
            # instead of this answer.
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._send_json(
                {"ok": False, "error": f"image is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        data = self.rfile.read(length)
        try:
            ref = store_upload(artwork_dir_for(self._ctx.output_path), data)
        except UploadError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except OSError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "ref": ref})

    def _send_upload(self, name: str) -> None:
        """Serve one uploaded image. Content-addressed, so cacheable forever."""
        path = upload_path(artwork_dir_for(self._ctx.output_path), "upload:" + name)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_image_file(path, "max-age=31536000, immutable")

    def _send_image_file(self, path: Path, cache_control: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_chapters(self) -> None:
        """Embed chapter markers, chapter art and the episode cover into the MP3.

        Exactly what ``setlist-maker chapters`` does, from the *saved* tracklist
        (the page saves first). Refused while a run is live -- it is reading the
        same file -- and one at a time: mutagen rewrites the file in place.
        """
        ctx = self._ctx
        if ctx.live is not None:
            self._send_json({"ok": False, "error": "still identifying"}, HTTPStatus.CONFLICT)
            return
        audio = ctx.audio_path
        if audio is None or not audio.exists() or audio.suffix.lower() != ".mp3":
            self._send_json(
                {"ok": False, "error": "chapter markers need the set's MP3"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if not any(not t.is_unidentified for t in ctx.tracklist.tracks if not t.rejected):
            self._send_json(
                {"ok": False, "error": "no identified tracks to mark"}, HTTPStatus.BAD_REQUEST
            )
            return
        if not _EMBED_LOCK.acquire(blocking=False):
            self._send_json({"ok": False, "error": "already embedding"}, HTTPStatus.CONFLICT)
            return
        try:
            # Imported here: cli imports this module, so a top-level import is a cycle.
            from setlist_maker.cli import embed_chapters_for_tracklist

            # A save landing mid-embed would change the tracklist being read.
            with ctx.state_lock:
                chapters, images, cover = embed_chapters_for_tracklist(
                    ctx.tracklist, audio, fetch_art=True, tracklist_path=ctx.output_path
                )
        except Exception as exc:  # a handler that raises sends no response at all
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        finally:
            _EMBED_LOCK.release()
        self._send_json({"ok": True, "chapters": chapters, "images": images, "cover": cover})

    def _send_peaks(self) -> None:
        """The waveform (#57): int8 min/max pairs, computed once per file (peaks.py)."""
        audio_path = self._ctx.audio_path
        if audio_path is None or not audio_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "audio not found")
            return
        try:
            body = peaks_for(audio_path, probe_duration_seconds(audio_path))
        except (OSError, subprocess.CalledProcessError):
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "could not read the audio")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

    resolved_audio = resolve_audio_path(audio_path, output_path)
    ctx = EditorContext(
        tracklist=tracklist,
        output_path=output_path,
        corrections_db=corrections_db,
        audio_path=resolved_audio,
        progress_path=progress_path_for(output_path, resolved_audio),
        sampler=ShazamSampler(resolved_audio) if resolved_audio else None,
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


class WatchSession:
    """``identify --watch``: the editor's server, opened before the run it watches.

    Starts serving in a background thread in live mode, so the page follows
    the run as it probes; when the run finishes, :meth:`finish` hands the same
    server -- and the same browser tab -- the finished tracklist, and it is the
    ordinary editor from then on. The page notices the switch on its next poll.
    """

    def __init__(
        self,
        audio_path: Path,
        output_path: Path,
        engine_config: EngineConfig | None = None,
        sampling: bool = True,
        open_browser: bool = True,
    ):
        self.requests = SampleRequests() if sampling else None
        self.ctx = EditorContext(
            tracklist=Tracklist(source_file=audio_path.name),
            output_path=output_path,
            corrections_db=None,
            audio_path=audio_path,
            progress_path=progress_path_for(output_path, audio_path),
            live=LiveRun(
                source_file=audio_path.name,
                engine_config=engine_config,
                requests=self.requests,
                duration_hint=probe_duration_seconds(audio_path),
            ),
        )
        self.httpd = create_server(self.ctx)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        print(f"\nWatching in your browser: {self.url}")
        if open_browser:
            webbrowser.open(self.url)

    def finish(
        self,
        tracklist: Tracklist,
        output_path: Path,
        use_corrections: bool = True,
        audio_path: Path | None = None,
    ) -> None:
        """Turn the watch page into the editor, then serve until Done or Ctrl-C."""
        if self.requests is not None:
            self.requests.close()
        ctx = self.ctx
        if ctx.closed:
            self.httpd.server_close()
            return
        corrections_db = CorrectionsDB() if use_corrections else None
        if corrections_db:
            applied = corrections_db.apply_corrections(tracklist)
            if applied > 0:
                print(f"Applied {applied} learned correction(s) from previous sessions.")
        resolved_audio = resolve_audio_path(audio_path, output_path)
        # Everything the editor needs is in place before `live` is cleared,
        # since clearing it is what flips the handler into edit mode.
        ctx.corrections_db = corrections_db
        ctx.output_path = output_path
        ctx.audio_path = resolved_audio
        ctx.progress_path = progress_path_for(output_path, resolved_audio)
        # Paced from now: the run's last Shazam call may have been moments ago.
        ctx.sampler = (
            ShazamSampler(resolved_audio, last_call=time.monotonic()) if resolved_audio else None
        )
        with ctx.state_lock:  # one snapshot for /api/tracklist
            ctx.tracklist = tracklist
            ctx.revision += 1  # every index the page held named a live guess
            ctx.live = None
        print(f"\nEditing in your browser: {self.url}\n(Press Ctrl-C here to stop.)")
        try:
            while self.thread.is_alive():
                self.thread.join(0.5)
        except KeyboardInterrupt:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()

    def stop(self) -> None:
        """Shut down without editing -- the run failed, or was aborted."""
        if self.requests is not None:
            self.requests.close("identification stopped")
        if not self.ctx.closed:
            self.httpd.shutdown()
        self.httpd.server_close()
