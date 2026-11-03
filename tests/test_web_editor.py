"""Tests for the web editor (setlist_maker.web_editor)."""

import json
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from importlib.resources import files

import pytest

WEB = files("setlist_maker") / "web"
# Copied from npm and checked by hash (web/LICENSES.md), not by the import test.
VENDORED = {"preact.js", "preact_hooks.js", "htm.js"}


def _page_source():
    """The page as the browser gets it: markup, stylesheet and every module."""
    parts = [(files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")]
    parts += [f.read_text(encoding="utf-8") for f in sorted(WEB.iterdir(), key=lambda f: f.name)]
    return "\n".join(parts)


def test_every_element_lookup_exists():
    """A module that looks up an id the markup no longer has fails only in the
    browser, often inside a try that swallows it: #58 removed #tl-hint while
    loadProbes() still wrote to it, which emptied the timeline's pins."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for f in sorted(WEB.glob("*.js")):
        for name in re.findall(r'getElementById\("([^"]+)"\)', f.read_text(encoding="utf-8")):
            assert name in ids, f"{f.name} looks up #{name}, which the page does not have"


def test_page_wiring():
    """Substring-level guards for decisions a refactor could silently undo."""
    src = _page_source()
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    assert '<script src="/timeline.js"></script>' in html
    assert '<script type="module" src="/web/main.js"></script>' in html
    for endpoint in (
        "/api/tracklist",
        "/api/save",
        "/api/done",
        "/api/audio",
        "/api/probes",
        "/api/artwork?index=",
        "/api/artwork/options?index=",
        "/api/artwork/source?index=",
    ):
        assert endpoint in src
    for element_id in (
        "timeline",
        "art-dialog",
        "artwork-grid",
        "art-cover",
        "art-prev",
        "art-next",
        "art-url",
    ):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"
    assert '<textarea id="summary"' in html and "Add a description for this set" in html
    assert "summary: summaryEl.value" in src  # the description is in the save payload
    assert "addBelow(" in src and "timefield" in src and "MM:SS" in src  # insert + its time

    # Dark mode is tokens + a prefers-color-scheme block; native controls follow (#34).
    assert '<meta name="color-scheme" content="light dark">' in html
    assert "color-scheme: light dark" in src and "@media (prefers-color-scheme: dark)" in src
    # The app shell (#54): the window never scrolls, only the panes do, and
    # the player is a grid row rather than a fixed bar covering the list.
    assert "html, body { height:100%; overflow:hidden; }" in src
    assert "#player { position:fixed" not in src
    for element_id in (
        "toolbar",
        "listpane",
        "list-head",
        "side",
        "tabs",
        "panel-track",
        "panel-set",
        "panel-queue",
        "split-tl",
        "split-side",
        "keys-dialog",  # the shortcut sheet (#58)
        "ask",  # ask(), the in-app confirm (#61)
        "keys-btn",
        "pfollow",
    ):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"
    # The header is a sibling of #list: Preact owns everything inside #list.
    assert html.index('id="list-head"') < html.index('<main id="list"')
    # The keys are one table, and the sheet is built from it, so the old
    # prose hint under the timeline is gone rather than kept in step (#58).
    assert 'id="tl-hint"' not in html and 'role="listbox"' in html

    # Artwork. A saved edit changes the composite, so thumbs cache-bust on a
    # version bumped at save, or Chrome reuses the pre-edit image.
    assert '"&v=" + S.artVersion' in src and "S.artVersion++" in src
    # The `background` shorthand resets background-size:cover: a corner crop
    # with the composite's lower third off-screen.
    assert "pt.style.background =" not in src and "style=${{ backgroundImage: bg }}" in src
    # A row's thumb stops observing when it unmounts, or every edit would leak one.
    assert "return () => { artObserver.unobserve(node)" in src
    # 0 is a real index; an unsaved row has none and must not request art.
    assert "t.index !== undefined && t.index !== null" in src
    # Dialogs are native <dialog>s opened modally (#61): the browser traps focus
    # and handles Escape. The keyboard map stands down while one is open, and
    # the page asks its questions in-app, never with the browser's dialogs.
    assert html.count("<dialog id=") == 3 and "showModal()" in src
    assert "if (dialogOpen()) return;" in src
    for native in ("confirm(", "alert(", "prompt("):
        assert native not in src, native
    assert "[hidden] { display:none !important; }" in src  # beats author display rules
    # The toast is a popover, so it is shown above a modal dialog opened earlier.
    assert '<div id="toast" popover="manual"' in html and "t.showPopover()" in src
    # An unsaved pick wins over saved state in the panel, and is the only reason
    # a row sends coverart_url -- sending it for every row re-pins art (#30).
    assert "if (t._art) {" in src and "if (!t._art) {" in src
    assert "if (t._art) edit.coverart_url = t.coverart_url;" in src
    assert "episode_cover: !!t.episode_cover" in src
    assert "&artist=" in src and "&title=" in src  # the picker searches the live text


def test_every_module_import_is_exported():
    """No bundler checks the page's imports: a module importing a name its source
    doesn't export fails only in the browser, and takes the whole page with it."""
    exports, imports = {}, {}
    for f in WEB.iterdir():
        if not f.name.endswith(".js") or f.name in VENDORED:
            continue
        text = f.read_text(encoding="utf-8")
        exports[f.name] = set(
            re.findall(r"^export (?:async )?(?:function|const|let) (\w+)", text, re.M)
        )
        for names in re.findall(r"^export \{([^}]*)\};", text, re.M):
            exports[f.name] |= {n.strip() for n in names.split(",") if n.strip()}
        imports[f.name] = re.findall(r'^import \{([^}]*)\} from "\./(\w+\.js)";', text, re.M)
        for side_effect in re.findall(r'^import "\./(\w+\.js)";', text, re.M):
            imports[f.name].append(("", side_effect))
    assert "main.js" in exports
    for name, stmts in imports.items():
        for names, source in stmts:
            assert source in exports, f"{name} imports from missing {source}"
            for imported in (n.split(" as ")[0].strip() for n in names.split(",") if n.strip()):
                assert imported in exports[source], f"{name}: {source} does not export {imported}"


def test_vendored_files_match_their_recorded_hashes():
    """Preact and htm are copied from npm byte for byte; an edit to one would
    otherwise pass unnoticed as "the library"."""
    import hashlib

    table = (WEB / "LICENSES.md").read_text(encoding="utf-8")
    recorded = dict(re.findall(r"^\| `([\w.]+)` \|.*\| `([0-9a-f]{64})` \|$", table, re.M))
    assert set(recorded) == VENDORED
    for name, digest in recorded.items():
        assert hashlib.sha256((WEB / name).read_bytes()).hexdigest() == digest, name


def test_web_assets_are_served_and_nothing_else(sample_tracklist, tmp_path):
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(base + "/web/main.js") as r:
            assert r.headers["Content-Type"].startswith("text/javascript")
        with urllib.request.urlopen(base + "/web/editor.css") as r:
            assert r.headers["Content-Type"].startswith("text/css")
        for bad in ("/web/nope.js", "/web/../web_editor.py", "/web/%2e%2e/cli.js", "/web/main.py"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404, bad


def test_page_asset_has_player_controls():
    """The transport controls and their IDs are the contract the script binds to."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    for element_id in ("pprev", "pback15", "pplay", "pfwd15", "pnext", "ppos", "pcount", "seek"):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"


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


def test_post_save_keeps_an_inserted_row_that_has_only_a_title(sample_tracklist, tmp_path):
    """The one-move repro, end to end through the page's save (#44).

    "＋ Add below" inserts a blank row; typing only the title -- because that is
    all you know -- used to write a row the reader could not see, so it was gone
    the next time the set was opened, with nothing said.
    """
    from setlist_maker.web_editor import EditorContext

    ctx = EditorContext(
        tracklist=sample_tracklist,
        output_path=tmp_path / "set_tracklist.md",
        corrections_db=None,
        audio_path=None,
    )
    before = len(sample_tracklist.tracks)  # apply_edits inserts into this list in place
    payload = json.dumps(
        {
            "tracks": [
                {"index": i, "artist": t.artist, "title": t.title, "rejected": False}
                for i, t in enumerate(sample_tracklist.tracks)
            ]
            + [{"artist": "  ", "title": "Titled Only", "timestamp": 90, "rejected": False}],
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

    from setlist_maker.editor import parse_markdown_tracklist

    md = (tmp_path / "set_tracklist.md").read_text()
    reopened = parse_markdown_tracklist(md)

    assert "1. **Daft Punk** - Around the World (0:00)" in md
    assert "2. *Unknown artist* - Titled Only (1:30)" in md
    assert (90, "", "Titled Only") in [(t.timestamp, t.artist, t.title) for t in reopened.tracks]
    assert len(reopened.tracks) == before + 1


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
        ("/api/artwork/source?index=0", "GET", None),
        ("/api/probes", "GET", None),
        ("/timeline.js", "GET", None),
        ("/api/save", "POST", b'{"tracks": []}'),
        ("/api/done", "POST", b"{}"),
        ("/api/cover", "GET", None),
        ("/api/upload/" + "0" * 24 + ".jpg", "GET", None),
        ("/api/upload", "POST", b"GIF89a"),
        ("/api/chapters", "POST", b"{}"),
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


@pytest.mark.parametrize("endpoint", ["options", "source"])
@pytest.mark.parametrize("qs", ["index=99", "index=-1", "index=abc", "", "index=2"])
def test_artwork_endpoints_404_where_the_composite_does(
    sample_tracklist, tmp_path, offline_artwork, qs, endpoint
):
    """Every artwork endpoint answers for exactly the same set of tracks --
    index 2 in the fixture is unidentified, which chapters skips too."""
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/artwork/{endpoint}?{qs}")
    assert exc.value.code == 404


def test_artwork_source_serves_the_cover_without_the_overlay(
    sample_tracklist, tmp_path, monkeypatch
):
    """The page's thumbnails all show this (#60), so it is the art as found."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    png = b"\x89PNG\r\n\x1a\n" + b"cover"
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: png,
    )
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(f"{base}/api/artwork/source?index=0") as r:
            assert r.headers["Content-Type"] == "image/png"
            assert "no-store" in r.headers.get("Cache-Control", "")
            assert r.read() == png


def test_a_track_with_no_art_anywhere_404s_only_from_the_source(
    sample_tracklist, tmp_path, offline_artwork
):
    """The composite draws the gradient card itself; the page draws its own."""
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(f"{base}/api/artwork?index=0") as r:
            assert r.status == 200
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/artwork/source?index=0")
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


def _write_progress(path, probes, duration=900.0):
    path.write_text(json.dumps({"version": 2, "audio_duration": duration, "probes": probes}))


def test_probes_to_api_flattens_results_and_keeps_asking_order():
    from setlist_maker.boundary import Probe
    from setlist_maker.web_editor import probes_to_api

    probes = [
        Probe(
            t=450.0,
            window=30.0,
            purpose="coverage",
            result={
                "artist": "A",
                "title": "a",
                "confidence": 0.66,
                "coverart_url": "https://x/a.jpg",
            },
        ),
        Probe(t=100.0, window=12.0, purpose="refine", result=None),
    ]
    api = probes_to_api(probes, 900.0)
    assert api["duration"] == 900.0
    # Not sorted: the order the engine asked in is what a replay shows.
    assert [p["t"] for p in api["probes"]] == [450.0, 100.0]
    assert api["probes"][0] == {
        "t": 450.0,
        "window": 30.0,
        "purpose": "coverage",
        "artist": "A",
        "title": "a",
        "confidence": 0.66,
        "coverart_url": "https://x/a.jpg",
    }
    assert api["probes"][1]["artist"] is None


@pytest.mark.parametrize(
    "md_name,audio,expected",
    [
        ("2026-09-23-Keys-Lounge_tracklist.md", None, "2026-09-23-Keys-Lounge_progress.json"),
        ("renamed.md", "set.mp3", "set_progress.json"),
        ("renamed.md", None, "renamed_progress.json"),
    ],
)
def test_progress_path_sits_beside_the_tracklist(tmp_path, md_name, audio, expected):
    from setlist_maker.web_editor import progress_path_for

    audio_path = tmp_path / audio if audio else None
    assert progress_path_for(tmp_path / md_name, audio_path) == tmp_path / expected


def test_get_probes_serves_the_progress_file(sample_tracklist, tmp_path):
    progress = tmp_path / "set_progress.json"
    _write_progress(
        progress,
        [
            {
                "t": 10.0,
                "window": 30.0,
                "purpose": "coverage",
                "result": {"artist": "Daft Punk", "title": "Around the World"},
            }
        ],
    )
    ctx = _ctx(sample_tracklist, tmp_path)
    ctx.progress_path = progress
    with running_server(ctx) as base:
        with urllib.request.urlopen(base + "/api/probes") as r:
            data = json.loads(r.read())
    assert data["duration"] == 900.0
    assert data["probes"][0]["artist"] == "Daft Punk"


def test_get_probes_rereads_the_file_so_a_live_run_shows_up(sample_tracklist, tmp_path):
    progress = tmp_path / "set_progress.json"
    _write_progress(progress, [])
    ctx = _ctx(sample_tracklist, tmp_path)
    ctx.progress_path = progress
    with running_server(ctx) as base:
        with urllib.request.urlopen(base + "/api/probes") as r:
            assert json.loads(r.read())["probes"] == []
        _write_progress(
            progress, [{"t": 0.0, "window": 30.0, "purpose": "coverage", "result": None}]
        )
        with urllib.request.urlopen(base + "/api/probes") as r:
            assert len(json.loads(r.read())["probes"]) == 1


@pytest.mark.parametrize("content", [None, "{not json", '{"version": 2, "probes": [{}]}'])
def test_get_probes_degrades_to_none_without_a_usable_file(sample_tracklist, tmp_path, content):
    """A set identified before adaptive sampling has no probes to draw; that is
    not an error, and neither is a file the page cannot use."""
    progress = tmp_path / "set_progress.json"
    if content is not None:
        progress.write_text(content)
    ctx = _ctx(sample_tracklist, tmp_path)
    ctx.progress_path = progress
    with running_server(ctx) as base:
        with urllib.request.urlopen(base + "/api/probes") as r:
            assert json.loads(r.read()) == {
                "duration": None,
                "probes": [],
                "live": False,
                "can_sample": False,
            }


def test_timeline_script_is_served(sample_tracklist, tmp_path):
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with urllib.request.urlopen(base + "/timeline.js") as r:
            assert r.headers["Content-Type"].startswith("text/javascript")
            assert "findIssues" in r.read().decode()


# ---- moving an existing track's start (#55) ----


def test_apply_edits_moves_an_existing_start_and_resorts(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    praise = sample_tracklist.tracks[3]
    apply_edits(
        sample_tracklist,
        [{"index": 3, "artist": "Fatboy Slim", "title": "Praise You", "timestamp": 300.4}],
        None,
    )
    assert praise.timestamp == 300  # whole seconds: the markdown writes (m:ss)
    assert [t.timestamp for t in sample_tracklist.tracks] == [0, 180, 300, 360]
    assert sample_tracklist.tracks[2] is praise
    assert not praise.was_corrected  # a boundary is not a correction


@pytest.mark.parametrize("bad", [None, "1:23", -5, True, float("nan"), float("inf")])
def test_apply_edits_leaves_the_start_alone_on_a_bad_value(sample_tracklist, bad):
    from setlist_maker.web_editor import apply_edits

    edit = {"index": 1, "artist": "The Chemical Brothers", "title": "Block Rockin' Beats"}
    if bad is not None:
        edit["timestamp"] = bad
    apply_edits(sample_tracklist, [edit], None)
    assert [t.timestamp for t in sample_tracklist.tracks] == [0, 180, 360, 540]


def test_a_moved_track_keeps_its_art_pin_and_star_through_save_and_reload(
    sample_tracklist, tmp_path
):
    """The sidecar is joined to the markdown by timestamp: both are rewritten
    on save, so a moved start must carry its pinned art and its star along."""
    from setlist_maker.cli import _load_tracklist_with_artwork_urls
    from setlist_maker.editor import save_tracklist
    from setlist_maker.web_editor import apply_edits

    track = sample_tracklist.tracks[1]
    track.coverart_url = "https://example.com/picked.jpg"
    track.artwork_pinned = True
    track.is_episode_cover = True
    apply_edits(
        sample_tracklist,
        [{"index": 1, "artist": track.artist, "title": track.title, "timestamp": 200}],
        None,
    )
    path = tmp_path / "set_tracklist.md"
    save_tracklist(sample_tracklist, path, None)

    reloaded, _ = _load_tracklist_with_artwork_urls(path)
    moved = reloaded.tracks[1]
    assert (moved.timestamp, moved.title) == (200, "Block Rockin' Beats")
    assert moved.coverart_url == "https://example.com/picked.jpg"
    assert moved.artwork_pinned and moved.is_episode_cover


def test_a_merge_into_the_next_track_saves_one_track_at_the_merged_start(
    sample_tracklist, tmp_path
):
    """#56: the page rejects the merged row and sends the next track's start as
    the merged row's. The two share a second in memory, but the rejected row is
    in neither file, so the reload has one track there, with its art."""
    from setlist_maker.cli import _load_tracklist_with_artwork_urls
    from setlist_maker.editor import save_tracklist
    from setlist_maker.web_editor import apply_edits

    blip, nxt = sample_tracklist.tracks[1], sample_tracklist.tracks[2]
    nxt.coverart_url = "https://example.com/next.jpg"
    nxt.artwork_pinned = True
    apply_edits(
        sample_tracklist,
        [
            {"index": 1, "artist": blip.artist, "title": blip.title, "rejected": True},
            {"index": 2, "artist": nxt.artist, "title": nxt.title, "timestamp": blip.timestamp},
        ],
        None,
    )
    path = tmp_path / "set_tracklist.md"
    save_tracklist(sample_tracklist, path, None)

    reloaded, _ = _load_tracklist_with_artwork_urls(path)
    assert [(t.timestamp, t.title) for t in reloaded.tracks] == [
        (0, sample_tracklist.tracks[0].title),
        (180, nxt.title),
        (540, sample_tracklist.tracks[3].title),
    ]
    assert reloaded.tracks[1].coverart_url == "https://example.com/next.jpg"


def test_post_save_reports_the_revision_a_reorder_bumps(sample_tracklist, tmp_path):
    """A save that re-sorts the tracks bumps the revision; the response carries
    it so the page reloads instead of 409ing on its next save."""
    ctx = _ctx(sample_tracklist, tmp_path)
    rows = [
        {"index": i, "artist": t.artist, "title": t.title}
        for i, t in enumerate(sample_tracklist.tracks)
    ]

    def save(tracks, revision):
        req = urllib.request.Request(
            base + "/api/save",
            method="POST",
            data=json.dumps({"revision": revision, "tracks": tracks}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    with running_server(ctx) as base:
        same = save(rows, ctx.revision)
        assert same["ok"] and same["revision"] == 0  # nothing moved past anything
        rows[0]["timestamp"] = 400  # past two neighbours
        moved = save(rows, 0)
        assert moved["ok"] and moved["revision"] == 1
    assert [t.timestamp for t in sample_tracklist.tracks] == [180, 360, 400, 540]
