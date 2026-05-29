"""Browser-based tracklist editor served from a local HTTP server.

A presentation-layer alternative to the Textual TUI in ``editor.py``, opened
with ``--web-edit``. Reuses the ``Track`` / ``Tracklist`` model and
``CorrectionsDB`` and serves a single-page app (``web_editor.html``) plus a
small JSON + audio API bound to loopback (``127.0.0.1``).
"""

from setlist_maker.editor import CorrectionsDB, Tracklist


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


def apply_edits(
    tracklist: Tracklist,
    edits: list[dict],
    corrections_db: "CorrectionsDB | None",
) -> None:
    """Apply per-track edits and rejections in place, recording corrections.

    Mirrors ``editor._on_edit_complete`` + ``action_toggle_reject`` so the web
    editor learns corrections identically to the TUI. ``edits`` is the list
    sent by the page, each item keyed by the stable ``index``.
    """
    by_index = dict(enumerate(tracklist.tracks))
    for edit in edits:
        track = by_index.get(edit.get("index"))
        if track is None:
            continue
        new_artist = (edit.get("artist") or "").strip()
        new_title = (edit.get("title") or "").strip()
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
