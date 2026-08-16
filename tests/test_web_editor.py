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


def test_page_asset_has_player_controls():
    """The transport controls and their IDs are the contract the script binds to."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    for element_id in ("pprev", "pback15", "pplay", "pfwd15", "pnext", "ppos", "pcount", "seek"):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"


def test_page_follows_color_scheme():
    """Dark mode is tokens + a prefers-color-scheme block, and native controls
    follow via color-scheme (#34). Substring-level: there is no JS/CSS harness."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    assert '<meta name="color-scheme" content="light dark">' in html
    assert "color-scheme: light dark" in html  # on :root, for the seek slider et al.
    assert "@media (prefers-color-scheme: dark)" in html


def test_body_padding_clears_the_player_bar():
    """The list must not hide behind the fixed player.

    Substring-level, because there is no JS harness -- it catches the specific
    regression of growing the bar without growing the padding.
    """
    import re

    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    padding = re.search(r"body\s*\{[^}]*padding-bottom:\s*(\d+)px", html)
    assert padding, "body padding-bottom not found"
    assert int(padding.group(1)) >= 100, (
        f"body padding-bottom is {padding.group(1)}px; the reworked player bar is ~84px "
        f"and needs more clearance than that"
    )


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


def test_apply_edits_clears_stale_coverart_url_on_correction(sample_tracklist):
    """Correcting a track retires the Shazam art URL so the waterfall re-searches (#30)."""
    from setlist_maker.web_editor import apply_edits

    track = sample_tracklist.tracks[0]
    track.coverart_url = "https://cdn.shazam.com/wrong-album.jpg"

    apply_edits(sample_tracklist, [{"index": 0, "artist": "Justice", "title": "Genesis"}], None)

    assert track.coverart_url is None


def test_apply_edits_keeps_coverart_url_when_only_rejecting(sample_tracklist):
    """Rejecting a row re-sends its unchanged fields; that must not discard good art."""
    from setlist_maker.web_editor import apply_edits

    track = sample_tracklist.tracks[0]
    track.coverart_url = "https://cdn.shazam.com/right-album.jpg"

    apply_edits(
        sample_tracklist,
        [{"index": 0, "artist": "Daft Punk", "title": "Around the World", "rejected": True}],
        None,
    )

    assert track.rejected is True
    assert track.coverart_url == "https://cdn.shazam.com/right-album.jpg"


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
    """One paragraph is house style, not a format constraint (see #16).

    Before the description was fenced, collapsing line breaks is what kept the
    markdown round-trip lossless. It round-trips either way now — so don't
    "fix" this test to preserve the breaks without deciding to change the shape
    of every saved description along with it.
    """
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


def test_post_save_survives_a_track_shaped_description(sample_tracklist, tmp_path):
    """The issue's own repro, end to end through the page's save (#16).

    Typing a numbered, track-looking line into the description used to cost the
    user the description *and* add a track they never played, the next time the
    set was opened.
    """
    from setlist_maker.web_editor import EditorContext

    description = "Closed with 1. **Test** - Song (0:00) — a phantom, once."
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
            "summary": description,
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
            assert json.loads(r.read())["ok"] is True

    from setlist_maker.editor import SUMMARY_OPEN_MARKER, parse_markdown_tracklist

    md = (tmp_path / "set_tracklist.md").read_text()
    reopened = parse_markdown_tracklist(md)

    assert md.count(SUMMARY_OPEN_MARKER) == 1
    assert reopened.summary == description
    assert len(reopened.tracks) == len(sample_tracklist.tracks)


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


@pytest.fixture
def offline_artwork(monkeypatch, tmp_path):
    """Isolate the artwork cache and keep it off the network."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: None,
    )


def test_artwork_endpoint_returns_jpeg(sample_tracklist, tmp_path, offline_artwork):
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(f"{base}/api/artwork?index=0") as r:
            assert r.status == 200
            assert r.headers["Content-Type"] == "image/jpeg"
            body = r.read()
    assert body.startswith(b"\xff\xd8")


def test_artwork_endpoint_404s_for_unidentified(sample_tracklist, tmp_path, offline_artwork):
    """Track 2 in the fixture is unidentified; chapters skips those, so does preview."""
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/artwork?index=2")
    assert exc.value.code == 404


@pytest.mark.parametrize("qs", ["index=99", "index=-1", "index=abc", ""])
def test_artwork_endpoint_404s_for_bad_index(sample_tracklist, tmp_path, offline_artwork, qs):
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/artwork?{qs}")
    assert exc.value.code == 404


def test_artwork_endpoint_is_not_browser_cached(sample_tracklist, tmp_path, offline_artwork):
    """no-store keeps an edited row from showing its pre-edit composite."""
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(f"{base}/api/artwork?index=0") as r:
            assert "no-store" in r.headers.get("Cache-Control", "")


def test_page_lazy_loads_composite_artwork():
    """The page requests the real composite per row and enlarges it on click."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    assert "/api/artwork?index=" in html
    assert "IntersectionObserver" in html  # visible rows generate first
    assert "artwork-overlay" in html  # click-to-enlarge target
    # The script looks the overlay up at top level, so the element must appear
    # before it. Placed after </script>, getElementById returns null and the
    # TypeError takes down the whole page -- which a substring check misses.
    assert html.index('id="artwork-overlay"') < html.index("<script>")
    # A saved edit changes the composite; without a cache-busting version param,
    # Chrome reuses the decoded image for the identical pre-edit URL and the
    # thumb never updates until a full page reload.
    assert '"&v=" + artVersion' in html
    assert "artVersion++" in html  # bumped on save so the next request is fresh
    # The `background` shorthand resets every background-* longhand, including
    # the background-size:cover that scales a 600px composite into a 42px thumb.
    # Setting it inline (or after background-size in a rule) renders the image at
    # natural size anchored top-left: a corner crop with the lower-third label --
    # the entire point of the composite -- off-screen.
    assert "thumb.style.background =" not in html
    assert "pt.style.background =" not in html
    assert "thumb.style.backgroundImage =" in html
    # Every render() rebuilds all rows; without disconnecting first, thumbs that
    # never scrolled into view leak one stale IntersectionObserver registration
    # per edit/reject/add on a detached element that will never be cleaned up.
    assert "artObserver.disconnect()" in html
    # An unsaved inserted row has no server index yet; observing/wiring it would
    # fire a wasted /api/artwork?index=undefined request. 0 is a legitimate
    # index, so the guard must check for undefined/null, not falsiness.
    assert "t.index !== undefined && t.index !== null" in html


def _request(base, path, host=None, method="GET", body=None):
    """Issue a request, optionally forging the Host header."""
    req = urllib.request.Request(base + path, method=method, data=body)
    if host is not None:
        req.add_header("Host", host)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req)


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/", "GET", None),
        ("/api/tracklist", "GET", None),
        ("/api/audio", "GET", None),
        ("/api/artwork?index=0", "GET", None),
        ("/api/artwork/options?index=0", "GET", None),
        ("/api/save", "POST", b'{"tracks": []}'),
        ("/api/done", "POST", b"{}"),
    ],
)
def test_every_endpoint_rejects_a_foreign_host(
    sample_tracklist, tmp_path, offline_artwork, path, method, body
):
    """DNS rebinding must not reach any endpoint.

    Binding loopback stops other machines, not a page the user is already on:
    a hostile site can point its own name at 127.0.0.1, after which the browser
    treats this server as same-origin and lets the page READ /api/tracklist and
    /api/audio (the source recording) and POST to /api/save. A rebound request
    still carries the attacker's Host, which is what this rejects.

    /api/done is included deliberately -- without the guard a hostile page could
    shut the editor down mid-session.
    """
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _request(base, path, host="attacker.example", method=method, body=body)
    assert exc.value.code == 403


@pytest.mark.parametrize(
    "host_template", ["127.0.0.1:{port}", "localhost:{port}", "LocalHost:{port}"]
)
def test_loopback_hosts_are_accepted(sample_tracklist, tmp_path, host_template):
    """The names a browser actually sends must work, case-insensitively."""
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        port = base.rsplit(":", 1)[1]
        with _request(base, "/api/tracklist", host=host_template.format(port=port)) as r:
            assert r.status == 200


@pytest.mark.parametrize("bad_host", ["", "127.0.0.1", "localhost", "127.0.0.1:1", "127.0.0.1:"])
def test_host_must_carry_this_servers_port(sample_tracklist, tmp_path, bad_host):
    """A missing or mismatched port is rejected, not treated as loopback.

    Requiring the exact ephemeral port is what stops a rebinding page from
    forging a valid Host: it would have to guess the port as well as the name.
    """
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _request(base, "/api/tracklist", host=bad_host)
    assert exc.value.code == 403


# --- Artwork curation (#20) -------------------------------------------------


def test_apply_edits_pins_a_chosen_cover_after_the_correction(sample_tracklist, tmp_path):
    """Picking art and fixing a typo in one save must keep the art.

    apply_track_edit() clears coverart_url on a real correction (#30). The
    pick is applied after it, so the ordering inside apply_edits is what makes
    both intentions survive -- and the pin is what protects the art from the
    *next* correction, in a later session.
    """
    from setlist_maker.editor import CorrectionsDB
    from setlist_maker.web_editor import apply_edits

    db = CorrectionsDB(db_path=tmp_path / "corrections.json")
    sample_tracklist.tracks[0].coverart_url = "https://cdn.shazam.com/wrong-album.jpg"

    apply_edits(
        sample_tracklist,
        [
            {
                "index": 0,
                "artist": "Daft Punk",
                "title": "Around the World (Radio Edit)",
                "rejected": False,
                "coverart_url": "https://itunes/discovery.jpg",
            }
        ],
        db,
    )

    track = sample_tracklist.tracks[0]
    assert track.title == "Around the World (Radio Edit)"
    assert track.coverart_url == "https://itunes/discovery.jpg"
    assert track.artwork_pinned is True


def test_apply_edits_without_the_key_leaves_artwork_alone(sample_tracklist):
    """Rows the user never opened the picker on send no coverart_url at all,
    so an ordinary save must not re-pin art #30 means to drop."""
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.tracks[0].coverart_url = "https://cdn.shazam.com/wrong-album.jpg"
    apply_edits(
        sample_tracklist,
        [{"index": 0, "artist": "Justice", "title": "Genesis", "rejected": False}],
        None,
    )
    assert sample_tracklist.tracks[0].coverart_url is None
    assert sample_tracklist.tracks[0].artwork_pinned is False


def test_apply_edits_clearing_the_url_unpins_it(sample_tracklist):
    """ "Automatic" in the picker sends a null URL: back to the waterfall."""
    from setlist_maker.web_editor import apply_edits

    track = sample_tracklist.tracks[0]
    track.coverart_url = "https://itunes/discovery.jpg"
    track.artwork_pinned = True

    apply_edits(
        sample_tracklist,
        [
            {
                "index": 0,
                "artist": track.artist,
                "title": track.title,
                "rejected": False,
                "coverart_url": None,
            }
        ],
        None,
    )
    assert track.coverart_url is None
    assert track.artwork_pinned is False


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "data:image/png;base64,AAAA", "ftp://x/a.jpg", "nonsense"]
)
def test_apply_edits_refuses_a_non_http_artwork_url(sample_tracklist, url):
    """This URL is persisted to the sidecar and fetched by this process later,
    and urlopen's default opener would read file:// off the machine. It is
    rejected before anything is mutated, so a bad row cannot half-apply a save.
    """
    from setlist_maker.web_editor import apply_edits

    original_title = sample_tracklist.tracks[0].title
    with pytest.raises(ValueError, match="http"):
        apply_edits(
            sample_tracklist,
            [
                {"index": 0, "artist": "Changed", "title": "Changed", "rejected": False},
                {"index": 1, "artist": "x", "title": "y", "rejected": False, "coverart_url": url},
            ],
            None,
        )
    assert sample_tracklist.tracks[0].title == original_title, "nothing may be applied"


def test_apply_edits_episode_cover_is_exclusive(sample_tracklist):
    """One cover per set, enforced server-side so a stale page cannot leave two."""
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.tracks[0].is_episode_cover = True

    def edit(i, **extra):
        t = sample_tracklist.tracks[i]
        return {"index": i, "artist": t.artist, "title": t.title, "rejected": False, **extra}

    apply_edits(sample_tracklist, [edit(0, episode_cover=True), edit(1, episode_cover=True)], None)

    # The last one the payload marks wins; every other track is cleared.
    assert [t.is_episode_cover for t in sample_tracklist.tracks] == [False, True, False, False]


def test_apply_edits_absent_episode_cover_key_changes_nothing(sample_tracklist):
    """An older client that does not send the key must not clear the choice."""
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.tracks[1].is_episode_cover = True
    apply_edits(
        sample_tracklist,
        [{"index": 0, "artist": "Daft Punk", "title": "Around the World", "rejected": False}],
        None,
    )
    assert sample_tracklist.tracks[1].is_episode_cover is True


def test_tracklist_to_api_exposes_the_episode_cover(sample_tracklist):
    from setlist_maker.web_editor import tracklist_to_api

    sample_tracklist.tracks[1].is_episode_cover = True
    api = tracklist_to_api(sample_tracklist)
    assert api["tracks"][1]["episode_cover"] is True
    assert api["tracks"][0]["episode_cover"] is False


def test_artwork_options_lists_the_alternates(sample_tracklist, tmp_path, monkeypatch):
    from setlist_maker.artwork import ArtworkCandidate

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    sample_tracklist.tracks[0].coverart_url = "https://cdn.shazam.com/in-use.jpg"
    monkeypatch.setattr(
        "setlist_maker.web_editor.artwork_options",
        lambda artist, title: [
            # The source also offers the URL already in use: one tile, not two.
            ArtworkCandidate("iTunes", "https://cdn.shazam.com/in-use.jpg", "Discovery"),
            ArtworkCandidate("Deezer", "https://dz/alive.jpg", "Alive 2007"),
        ],
    )

    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(base + "/api/artwork/options?index=0") as r:
            data = json.loads(r.read())

    assert [c["url"] for c in data["candidates"]] == [
        "https://cdn.shazam.com/in-use.jpg",
        "https://dz/alive.jpg",
    ]
    # The one in use is offered first and flagged, so the grid can show which is which.
    assert data["candidates"][0]["source"] == "In use"
    assert data["candidates"][0]["current"] is True
    assert data["candidates"][1]["current"] is False


def test_artwork_options_survives_a_failing_lookup(sample_tracklist, tmp_path, monkeypatch):
    """A handler that raises sends no response at all and dumps a traceback
    over the CLI's output; the page has to get an answer either way."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def boom(artist, title):
        raise RuntimeError("sources unreachable")

    monkeypatch.setattr("setlist_maker.web_editor.artwork_options", boom)

    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(base + "/api/artwork/options?index=0") as r:
            assert r.status == 200
            data = json.loads(r.read())
    assert data["candidates"] == []
    assert "unreachable" in data["error"]


@pytest.mark.parametrize("qs", ["index=99", "index=-1", "index=abc", "", "index=2"])
def test_artwork_options_404s_where_the_composite_does(sample_tracklist, tmp_path, qs):
    """Both artwork endpoints answer for exactly the same set of tracks --
    index 2 in the fixture is unidentified, which chapters skips too."""
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/artwork/options?{qs}")
    assert exc.value.code == 404


def test_save_round_trips_curation_into_the_sidecar(sample_tracklist, tmp_path):
    """The choices have to survive into a later, separate `chapters` process,
    which reads them back off disk by timestamp."""
    from setlist_maker.cli import _load_tracklist_with_artwork_urls
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
                {
                    "index": i,
                    "artist": t.artist,
                    "title": t.title,
                    "rejected": False,
                    "episode_cover": i == 1,
                    **({"coverart_url": "https://itunes/discovery.jpg"} if i == 1 else {}),
                }
                for i, t in enumerate(sample_tracklist.tracks)
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
            assert json.loads(r.read())["ok"] is True

    reloaded, _urls = _load_tracklist_with_artwork_urls(tmp_path / "set_tracklist.md")
    starred = [t for t in reloaded.tracks if t.is_episode_cover]
    assert [t.title for t in starred] == ["Block Rockin' Beats"]
    assert starred[0].coverart_url == "https://itunes/discovery.jpg"
    assert starred[0].artwork_pinned is True


def test_page_has_the_artwork_picker():
    """Substring-level, as every page assertion here is: there is no JS harness."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    assert "/api/artwork/options?index=" in html
    for element_id in ("artwork-panel", "artwork-grid", "art-choose", "art-cover", "art-url"):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"
    # Same reason as #artwork-overlay: the script resolves these at top level,
    # so markup placed after </script> makes getElementById return null and the
    # TypeError takes down the whole page -- which a substring check misses.
    assert html.index('id="artwork-panel"') < html.index("<script>")
    # The panel is full of controls now, so only a click on the backdrop itself
    # may dismiss it.
    assert "if (e.target === overlay) closeArtwork()" in html
    # Candidate tiles are BUTTONs and the keyboard guard exempts only inputs,
    # so without this the arrows seek the audio while covers are being browsed.
    assert 'if (overlay.classList.contains("open")) return;' in html
    # An author display rule beats the UA stylesheet, so the flex containers
    # would ignore the hidden attribute without this.
    assert "[hidden] { display:none !important; }" in html
    # The toast must outrank the overlay or it is raised behind the backdrop.
    assert "z-index:60" in html


def test_apply_edits_refuses_the_star_on_a_rejected_track(sample_tracklist):
    """A rejected track cannot be the episode cover, and must not clear the choice.

    to_json() drops rejected tracks, so a star on one could never be stored --
    but accepting it would still clear whichever track legitimately held it,
    leaving the set with no cover at all and nothing to report the loss.
    """
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.tracks[0].is_episode_cover = True

    def edit(i, **extra):
        t = sample_tracklist.tracks[i]
        return {"index": i, "artist": t.artist, "title": t.title, **extra}

    apply_edits(
        sample_tracklist,
        [edit(0, rejected=False, episode_cover=True), edit(1, rejected=True, episode_cover=True)],
        None,
    )

    assert [t.is_episode_cover for t in sample_tracklist.tracks] == [True, False, False, False]
    # ...and it survives the round trip, which is where the loss would have shown up
    assert [t["episode_cover"] for t in sample_tracklist.to_json()].count(True) == 1


def test_apply_edits_unstars_a_track_that_becomes_rejected(sample_tracklist):
    """Rejecting the starred track clears its star rather than orphaning it."""
    from setlist_maker.web_editor import apply_edits

    track = sample_tracklist.tracks[1]
    track.is_episode_cover = True
    apply_edits(
        sample_tracklist,
        [
            {
                "index": 1,
                "artist": track.artist,
                "title": track.title,
                "rejected": True,
                "episode_cover": True,
            }
        ],
        None,
    )
    assert track.is_episode_cover is False


def test_artwork_options_searches_the_live_artist_and_title(
    sample_tracklist, tmp_path, monkeypatch
):
    """The picker searches what the page shows, not what is saved.

    Correcting a misidentification and then fixing its cover is one workflow:
    searching the stale name would offer covers for the song being corrected
    away, and the user would pin one of them.
    """
    from setlist_maker.artwork import ArtworkCandidate

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    asked = []

    def fake_options(artist, title):
        asked.append((artist, title))
        return [ArtworkCandidate("iTunes", "https://x/a.jpg", "")]

    monkeypatch.setattr("setlist_maker.web_editor.artwork_options", fake_options)

    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(
            base + "/api/artwork/options?index=0&artist=Justice&title=Genesis"
        ) as r:
            assert r.status == 200
            r.read()
        # ...and it falls back to the saved values when the page sends none
        with urllib.request.urlopen(base + "/api/artwork/options?index=0") as r:
            r.read()

    assert asked == [("Justice", "Genesis"), ("Daft Punk", "Around the World")]


def test_page_panel_survives_an_unsaved_pick_and_sends_it():
    """Substring-level guards for the two places live page state must win.

    The row thumb already previews an unsaved pick; the panel that opens from
    that same thumb has to agree, or reopening shows the pre-pick composite and
    outlines the cover the user just replaced as the one in use.
    """
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    # showArtwork branches on the unsaved-pick flag rather than always
    # requesting the server composite, which is built from saved state.
    assert "if (t._art) {" in html
    # ...and the server's `current` flag is only adopted when there is no pick.
    assert "if (!t._art) {" in html
    # The save payload carries a pick only for rows that have one: sending it
    # for every row would re-pin art apply_track_edit() means to drop (#30).
    assert "if (t._art) edit.coverart_url = t.coverart_url;" in html
    assert "episode_cover: !!t.episode_cover" in html
    # The picker searches the live text, not the saved identification.
    assert "&artist=" in html and "&title=" in html
