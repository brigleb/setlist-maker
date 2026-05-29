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


def test_apply_edits_inserts_new_track_in_chronological_order(sample_tracklist):
    """An edit without an index is a new track; it lands sorted by timestamp."""
    from setlist_maker.web_editor import apply_edits

    # fixture timestamps: 0, 180, 360, 540 — insert one at 270 (between 180 and 360)
    apply_edits(
        sample_tracklist,
        [{"artist": "Justice", "title": "Genesis", "timestamp": 270, "rejected": False}],
        None,
    )

    timestamps = [t.timestamp for t in sample_tracklist.tracks]
    assert timestamps == [0, 180, 270, 360, 540]
    inserted = sample_tracklist.tracks[2]
    assert inserted.artist == "Justice"
    assert inserted.title == "Genesis"


def test_apply_edits_appends_new_track_after_last(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    apply_edits(
        sample_tracklist,
        [{"artist": "Bonobo", "title": "Kerala", "timestamp": 600}],
        None,
    )
    assert [t.timestamp for t in sample_tracklist.tracks] == [0, 180, 360, 540, 600]
    assert sample_tracklist.tracks[-1].artist == "Bonobo"


def test_apply_edits_mixes_new_track_with_existing_index_edits(sample_tracklist):
    """A new track (no index) must not disturb index-based mapping of existing edits."""
    from setlist_maker.web_editor import apply_edits

    apply_edits(
        sample_tracklist,
        [
            {"index": 0, "artist": "Daft Punk", "title": "One More Time", "rejected": False},
            {"artist": "Aphex Twin", "title": "Windowlicker", "timestamp": 90},  # new, no index
            {"index": 3, "artist": "Fatboy Slim", "title": "Praise You", "rejected": True},
        ],
        None,
    )

    assert [t.timestamp for t in sample_tracklist.tracks] == [0, 90, 180, 360, 540]
    # existing edits applied by stable index, not payload position
    by_ts = {t.timestamp: t for t in sample_tracklist.tracks}
    assert by_ts[0].title == "One More Time"
    assert by_ts[540].rejected is True
    assert by_ts[90].artist == "Aphex Twin"


def test_apply_edits_new_track_records_no_correction(sample_tracklist, tmp_path):
    """Inserting a track is not a Shazam correction; nothing is learned."""
    from setlist_maker.editor import CorrectionsDB
    from setlist_maker.web_editor import apply_edits

    db = CorrectionsDB(db_path=tmp_path / "corrections.json")
    apply_edits(
        sample_tracklist,
        [{"artist": "Moderat", "title": "A New Error", "timestamp": 270}],
        db,
    )

    inserted = next(t for t in sample_tracklist.tracks if t.timestamp == 270)
    assert inserted.was_corrected is False
    assert db.get_correction("Moderat", "A New Error") is None


def test_apply_edits_new_track_missing_timestamp_defaults_to_zero(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    apply_edits(sample_tracklist, [{"artist": "A", "title": "B"}], None)
    assert len(sample_tracklist.tracks) == 5
    new = next(t for t in sample_tracklist.tracks if t.artist == "A")
    assert new.timestamp == 0
    # stable sort keeps the pre-existing 0:00 track ahead of the appended one
    assert sample_tracklist.tracks[0].artist == "Daft Punk"
    assert sample_tracklist.tracks[1].artist == "A"


def test_apply_edits_sets_summary(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    apply_edits(sample_tracklist, [], None, summary="A deep house journey.")
    assert sample_tracklist.summary == "A deep house journey."


def test_apply_edits_clears_summary_when_blank(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.summary = "Original."
    apply_edits(sample_tracklist, [], None, summary="   ")
    assert sample_tracklist.summary is None


def test_apply_edits_clears_summary_when_none(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.summary = "Original."
    apply_edits(sample_tracklist, [], None, summary=None)
    assert sample_tracklist.summary is None


def test_apply_edits_leaves_summary_unchanged_when_omitted(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.summary = "Original summary."
    apply_edits(
        sample_tracklist,
        [{"index": 0, "artist": "Daft Punk", "title": "Around the World"}],
        None,
    )
    assert sample_tracklist.summary == "Original summary."


def test_apply_edits_normalizes_summary_whitespace(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    apply_edits(sample_tracklist, [], None, summary="Line one.\n\nLine two.   Extra")
    assert sample_tracklist.summary == "Line one. Line two. Extra"


def test_summary_round_trips_through_markdown(sample_tracklist):
    from setlist_maker.editor import parse_markdown_tracklist
    from setlist_maker.web_editor import apply_edits

    apply_edits(sample_tracklist, [], None, summary="A sweaty warehouse set.")
    reparsed = parse_markdown_tracklist(sample_tracklist.to_markdown())
    assert reparsed.summary == "A sweaty warehouse set."


def test_inserted_track_round_trips_through_markdown(sample_tracklist):
    """Insert -> to_markdown -> parse re-produces the track in chronological order."""
    from setlist_maker.editor import parse_markdown_tracklist
    from setlist_maker.web_editor import apply_edits

    apply_edits(
        sample_tracklist,
        [{"artist": "Caribou", "title": "Odessa", "timestamp": 270}],
        None,
    )
    reparsed = parse_markdown_tracklist(sample_tracklist.to_markdown())

    assert [t.timestamp for t in reparsed.tracks] == [0, 180, 270, 360, 540]
    third = reparsed.tracks[2]
    assert third.artist == "Caribou"
    assert third.title == "Odessa"


def test_page_has_insert_affordance_and_time_field():
    """The page exposes a way to add a track and edit a new row's timestamp."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    assert "addBelow(" in html  # per-row "+ Add below" handler
    assert "timefield" in html  # inline timestamp field for new rows
    assert "MM:SS" in html  # ...with an MM:SS placeholder


def test_page_has_editable_summary():
    """The description is an always-on textarea, has the empty-state placeholder,
    and is included in the save payload."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    assert '<textarea id="summary"' in html
    assert "Add a description for this set" in html  # empty-state placeholder
    assert "summary: summaryEl.value" in html  # included in the POST /api/save body


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


def test_post_save_writes_summary_to_markdown(sample_tracklist, tmp_path):
    from setlist_maker.web_editor import EditorContext

    ctx = EditorContext(
        tracklist=sample_tracklist,
        output_path=tmp_path / "set_tracklist.md",
        corrections_db=None,
        audio_path=None,
    )
    payload = json.dumps(
        {
            "tracks": [
                {"index": i, "artist": t.artist, "title": t.title, "rejected": False}
                for i, t in enumerate(sample_tracklist.tracks)
            ],
            "summary": "A sweaty warehouse set.",
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
    md = (tmp_path / "set_tracklist.md").read_text()
    assert "A sweaty warehouse set." in md


def test_post_save_absent_summary_leaves_existing_summary_unchanged(sample_tracklist, tmp_path):
    """A track-only save (no summary key) must not clear an existing summary."""
    from setlist_maker.web_editor import EditorContext

    sample_tracklist.summary = "Original summary."
    ctx = EditorContext(
        tracklist=sample_tracklist,
        output_path=tmp_path / "set_tracklist.md",
        corrections_db=None,
        audio_path=None,
    )
    payload = json.dumps(
        {
            "tracks": [
                {"index": i, "artist": t.artist, "title": t.title, "rejected": False}
                for i, t in enumerate(sample_tracklist.tracks)
            ]
            # "summary" key deliberately absent
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
    md = (tmp_path / "set_tracklist.md").read_text()
    assert "Original summary." in md


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


def test_post_done_shuts_server_down(sample_tracklist, tmp_path):
    """After /api/done the server stops serving and the thread exits."""
    from setlist_maker.web_editor import create_server

    httpd = create_server(_ctx(sample_tracklist, tmp_path))
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(base + "/api/done", data=b"", method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["ok"] is True
        thread.join(timeout=3)
        assert not thread.is_alive()  # serve_forever returned
    finally:
        httpd.server_close()


def test_run_web_editor_opens_browser_and_returns(monkeypatch, sample_tracklist, tmp_path):
    """run_web_editor opens the browser and returns once serving ends."""
    import setlist_maker.web_editor as web

    opened = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))
    # don't actually block: make serve_forever a no-op for this test
    monkeypatch.setattr(web.ThreadingHTTPServer, "serve_forever", lambda self: None)

    web.run_web_editor(
        sample_tracklist,
        tmp_path / "set_tracklist.md",
        use_corrections=False,
        audio_path=None,
        open_browser=True,
    )

    assert opened and opened[0].startswith("http://127.0.0.1:")
