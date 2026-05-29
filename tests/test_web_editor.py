"""Tests for the web editor (setlist_maker.web_editor)."""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from importlib.resources import files

import pytest


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


def test_post_save_writes_files_and_records_correction(sample_tracklist, tmp_path):
    from setlist_maker.editor import CorrectionsDB
    from setlist_maker.web_editor import EditorContext

    db = CorrectionsDB(db_path=tmp_path / "corrections.json")
    ctx = EditorContext(
        tracklist=sample_tracklist,
        output_path=tmp_path / "set_tracklist.md",
        corrections_db=db,
        audio_path=None,
    )
    payload = json.dumps(
        {
            "tracks": [
                {"index": 0, "artist": "Daft Punk", "title": "One More Time", "rejected": False},
                {"index": 3, "artist": "Fatboy Slim", "title": "Praise You", "rejected": True},
            ]
        }
    ).encode()

    with running_server(ctx) as base:
        req = urllib.request.Request(
            base + "/api/save",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            res = json.loads(r.read())

    assert res["ok"] is True
    assert res["rejected"] == 1  # the index-3 edit rejected one track
    assert res["edited"] == 1  # the index-0 title change counts as edited
    md = (tmp_path / "set_tracklist.md").read_text()
    assert "One More Time" in md
    assert "Praise You" not in md  # rejected, excluded from output
    assert (tmp_path / "set_tracklist.json").exists()
    assert db.get_correction("Daft Punk", "Around the World") == ("Daft Punk", "One More Time")


def test_get_audio_full_and_range(sample_tracklist, tmp_path):
    audio = tmp_path / "set.mp3"
    audio.write_bytes(bytes(range(256)))  # 256 deterministic bytes
    ctx = _ctx(sample_tracklist, tmp_path, audio_path=audio)

    with running_server(ctx) as base:
        # full request
        with urllib.request.urlopen(base + "/api/audio") as r:
            assert r.status == 200
            assert r.headers["Accept-Ranges"] == "bytes"
            assert len(r.read()) == 256
        # range request
        req = urllib.request.Request(base + "/api/audio", headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206
            assert r.headers["Content-Range"] == "bytes 0-99/256"
            data = r.read()
            assert len(data) == 100
            assert data == bytes(range(100))


def test_get_audio_404_when_missing(sample_tracklist, tmp_path):
    with running_server(_ctx(sample_tracklist, tmp_path, audio_path=None)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/api/audio")
        assert exc.value.code == 404


def test_get_audio_ignores_malformed_range(sample_tracklist, tmp_path):
    audio = tmp_path / "set.mp3"
    audio.write_bytes(bytes(range(256)))
    ctx = _ctx(sample_tracklist, tmp_path, audio_path=audio)
    with running_server(ctx) as base:
        req = urllib.request.Request(base + "/api/audio", headers={"Range": "bytes=abc-def"})
        with urllib.request.urlopen(req) as r:
            # malformed range -> fall back to full 200 response, no crash
            assert r.status == 200
            assert len(r.read()) == 256
