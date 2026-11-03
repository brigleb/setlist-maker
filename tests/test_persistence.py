"""Persistence that cannot lose user work (#64).

Atomic tracklist writes, a corrupt corrections file kept rather than wiped,
corrections under XDG_CONFIG_HOME, and /api/save safe against a double submit.
"""

import json
import os
import stat
import threading
import urllib.error
import urllib.request

import pytest

from setlist_maker import fileio
from setlist_maker.fileio import atomic_write_text
from setlist_maker.tracklist import CorrectionsDB, Track, Tracklist, save_tracklist
from tests.test_web_editor import _ctx, running_server


def _tracklist(title="Before"):
    return Tracklist(
        source_file="set.mp3",
        generated_on="2026-11-01 12:00",
        tracks=[Track(timestamp=0, artist="A", title=title)],
    )


# --- atomic writes -----------------------------------------------------------


def test_a_write_killed_midway_leaves_the_previous_tracklist(tmp_path, monkeypatch):
    md = tmp_path / "set_tracklist.md"
    save_tracklist(_tracklist("Before"), md)
    before_md, before_json = md.read_text(), md.with_suffix(".json").read_text()

    def dies(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(fileio.os, "replace", dies)
    with pytest.raises(OSError, match="disk full"):
        save_tracklist(_tracklist("After"), md)

    assert md.read_text() == before_md
    assert md.with_suffix(".json").read_text() == before_json
    assert sorted(p.name for p in tmp_path.iterdir()) == ["set_tracklist.json", "set_tracklist.md"]


def test_the_sidecar_is_written_before_the_markdown(tmp_path, monkeypatch):
    """If only one lands it must be the sidecar: an old sidecar under a new .md
    re-joins a corrected track to the Shazam URL it was corrected away from (#30)."""
    order = []
    real = fileio.os.replace
    monkeypatch.setattr(
        fileio.os, "replace", lambda src, dst: (order.append(os.path.basename(dst)), real(src, dst))
    )
    save_tracklist(_tracklist(), tmp_path / "set_tracklist.md")
    assert order == ["set_tracklist.json", "set_tracklist.md"]


def test_a_rewritten_file_keeps_its_permissions(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("old")
    path.chmod(0o640)
    atomic_write_text(path, "new")
    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_a_new_file_gets_ordinary_permissions_not_mkstemps(tmp_path):
    path = tmp_path / "fresh.md"
    atomic_write_text(path, "x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o666 & ~fileio._UMASK


def test_writing_through_a_symlink_keeps_the_link(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("old")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    atomic_write_text(link, "new")
    assert link.is_symlink()
    assert real.read_text() == "new"


# --- the corrections DB ------------------------------------------------------


@pytest.mark.parametrize("junk", ["{not json", "[1, 2, 3]", '{"corrections": []}', ""])
def test_a_corrupt_corrections_file_is_kept_not_wiped(tmp_path, junk, caplog):
    path = tmp_path / "corrections.json"
    path.write_text(junk)

    db = CorrectionsDB(db_path=path)
    db.add_correction("Wrong", "Name", "Right", "Name")
    db.save()

    kept = list(tmp_path.glob("corrections.json.corrupt-*"))
    assert len(kept) == 1
    assert kept[0].read_text() == junk
    assert list(json.loads(path.read_text())["corrections"]) == ["wrong|||name"]
    assert "kept it as" in caplog.text


def test_an_unreadable_corrections_file_is_never_overwritten(tmp_path):
    path = tmp_path / "corrections.json"
    path.write_text('{"corrections": {"a|||b": {"artist": "A", "title": "B"}}}')
    real_read = type(path).read_text

    def unreadable(self, *a, **kw):
        if self == path:
            raise PermissionError("denied")
        return real_read(self, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(path), "read_text", unreadable)
        db = CorrectionsDB(db_path=path)
        db.add_correction("X", "Y", "Z", "W")
        db.save()

    assert "a|||b" in json.loads(path.read_text())["corrections"]
    assert not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.parametrize("empty", ["{}", '{"corrections": {}}'])
def test_an_empty_corrections_file_is_just_empty(tmp_path, empty):
    path = tmp_path / "corrections.json"
    path.write_text(empty)
    assert CorrectionsDB(db_path=path).corrections == {}
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_a_good_corrections_file_loads(tmp_path):
    path = tmp_path / "corrections.json"
    path.write_text('{"corrections": {"a|||b": {"artist": "A", "title": "B"}}}')
    assert CorrectionsDB(db_path=path).corrections == {"a|||b": {"artist": "A", "title": "B"}}


def test_the_default_corrections_file_honours_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    db = CorrectionsDB()
    assert db.db_path == tmp_path / "setlist-maker" / "corrections.json"
    assert not db.db_path.parent.exists()  # constructing creates nothing
    db.save()
    assert db.db_path.exists()


# --- /api/save ---------------------------------------------------------------


def _post_save(base, payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + "/api/save", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _save_with_new_row(tracklist, revision=0, title="Row"):
    """What the page sends after "Add below": every row by index, plus the new one."""
    return {
        "revision": revision,
        "tracks": [
            {"index": i, "artist": t.artist, "title": t.title, "rejected": False}
            for i, t in enumerate(tracklist.tracks)
        ]
        + [{"artist": "New", "title": title, "timestamp": 90}],
    }


def _names(tracklist):
    return [(t.artist, t.title) for t in tracklist.tracks]


def test_concurrent_saves_of_one_inserted_row_insert_it_once(sample_tracklist, tmp_path):
    """A double submit: same payload, same revision, racing. One lands; the
    other is refused before its stale indices can rename anything."""
    payload = _save_with_new_row(sample_tracklist)
    expected = sorted(_names(sample_tracklist) + [("New", "Row")])
    ctx = _ctx(sample_tracklist, tmp_path)
    with running_server(ctx) as base:
        start = threading.Barrier(4)
        results = []

        def submit():
            start.wait()
            results.append(_post_save(base, payload))

        threads = [threading.Thread(target=submit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    assert sorted(status for status, _ in results) == [200, 409, 409, 409]
    assert sorted(_names(ctx.tracklist)) == expected
    assert (tmp_path / "set_tracklist.md").read_text().count("**New** - Row") == 1


def test_a_resent_save_is_refused_and_a_reloaded_one_lands(sample_tracklist, tmp_path):
    ctx = _ctx(sample_tracklist, tmp_path)
    payload = _save_with_new_row(sample_tracklist)
    with running_server(ctx) as base:
        assert _post_save(base, payload)[0] == 200
        before = _names(ctx.tracklist)
        status, body = _post_save(base, payload)
        assert status == 409 and "reload" in body["error"]
        assert _names(ctx.tracklist) == before  # nothing renamed

        with urllib.request.urlopen(base + "/api/tracklist") as r:
            reloaded = json.loads(r.read())
        assert reloaded["revision"] == 1
        assert _post_save(base, _save_with_new_row(ctx.tracklist, 1, "Second"))[0] == 200
    added = [title for artist, title in _names(ctx.tracklist) if artist == "New"]
    assert added == ["Row", "Second"]


def test_a_save_that_moves_nothing_keeps_the_revision(sample_tracklist, tmp_path):
    """Edits by stable index don't shift indices, so the page needn't reload."""
    ctx = _ctx(sample_tracklist, tmp_path)
    edits = {
        "revision": 0,
        "tracks": [{"index": 0, "artist": "Fixed", "title": "Name", "rejected": False}],
    }
    with running_server(ctx) as base:
        assert _post_save(base, edits)[0] == 200
        assert _post_save(base, edits)[0] == 200
    assert ctx.revision == 0


def test_a_page_that_sends_no_revision_still_saves(sample_tracklist, tmp_path):
    payload = _save_with_new_row(sample_tracklist)
    del payload["revision"]
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        assert _post_save(base, payload)[0] == 200


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json",
        b"[]",
        {"tracks": "nope"},
        {"tracks": [], "episode": "not an object"},
        {"tracks": [], "cover": "https://example.com/x.jpg"},
        {"tracks": [{"index": 0, "artist": "A", "title": "B", "coverart_url": "file:///etc"}]},
    ],
)
def test_a_bad_payload_is_a_400_and_saves_nothing(sample_tracklist, tmp_path, payload):
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        status, body = _post_save(base, payload)
    assert status == 400
    assert body["ok"] is False and body["error"]
    assert not (tmp_path / "set_tracklist.md").exists()
