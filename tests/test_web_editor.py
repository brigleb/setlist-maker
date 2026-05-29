"""Tests for the web editor (setlist_maker.web_editor)."""

import json
import threading
import urllib.request
from contextlib import contextmanager
from importlib.resources import files


def test_page_asset_exists_and_has_hooks():
    """The packaged HTML page exists and references the API + audio element."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    assert "/api/tracklist" in html
    assert "/api/save" in html
    assert "/api/done" in html
    assert "/api/audio" in html
    assert "<audio" in html


def test_tracklist_to_api_shape(sample_tracklist):
    from setlist_maker.web_editor import tracklist_to_api

    api = tracklist_to_api(sample_tracklist)
    assert api["source_file"] == "test_mix.mp3"
    assert "summary" in api
    assert len(api["tracks"]) == 4
    first = api["tracks"][0]
    assert first["index"] == 0
    assert first["artist"] == "Daft Punk"
    assert first["time"] == "0:00"
    assert first["is_unidentified"] is False
    # the third track is unidentified in the fixture
    assert api["tracks"][2]["is_unidentified"] is True


def test_apply_edits_updates_and_records_correction(sample_tracklist, tmp_path):
    from setlist_maker.editor import CorrectionsDB
    from setlist_maker.web_editor import apply_edits

    db = CorrectionsDB(db_path=tmp_path / "corrections.json")
    edits = [
        {"index": 0, "artist": "Daft Punk", "title": "Harder Better", "rejected": False},
        {
            "index": 1,
            "artist": "The Chemical Brothers",
            "title": "Block Rockin' Beats",
            "rejected": True,
        },
    ]
    apply_edits(sample_tracklist, edits, db)

    t0 = sample_tracklist.tracks[0]
    assert t0.title == "Harder Better"
    assert t0.was_corrected is True
    assert t0.original_title == "Around the World"
    assert sample_tracklist.tracks[1].rejected is True
    # the title edit was recorded for future learning
    assert db.get_correction("Daft Punk", "Around the World") == ("Daft Punk", "Harder Better")


def test_apply_edits_ignores_unknown_index(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    apply_edits(sample_tracklist, [{"index": 99, "artist": "X", "title": "Y"}], None)
    assert sample_tracklist.tracks[0].artist == "Daft Punk"  # unchanged


@contextmanager
def running_server(ctx):
    """Start the web editor server on an ephemeral port in a background thread."""
    from setlist_maker.web_editor import create_server

    httpd = create_server(ctx)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _ctx(tracklist, tmp_path, audio_path=None):
    from setlist_maker.web_editor import EditorContext

    return EditorContext(
        tracklist=tracklist,
        output_path=tmp_path / "set_tracklist.md",
        corrections_db=None,
        audio_path=audio_path,
    )


def test_get_root_serves_page(sample_tracklist, tmp_path):
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(base + "/") as r:
            body = r.read().decode()
            assert r.status == 200
            assert "<audio" in body


def test_get_tracklist_returns_json(sample_tracklist, tmp_path):
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(base + "/api/tracklist") as r:
            data = json.loads(r.read())
            assert data["source_file"] == "test_mix.mp3"
            assert len(data["tracks"]) == 4
