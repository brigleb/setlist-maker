"""The episode's title and artist (episode.py), and where they are written."""

import json
import shutil
import subprocess
import urllib.request

import pytest
from mutagen.id3 import TIT2, TPE1
from mutagen.mp3 import MP3

from setlist_maker.chapters import embed_chapters
from setlist_maker.editor import Track
from setlist_maker.episode import (
    clean_tag,
    config_path,
    default_title,
    episode_path_for,
    episode_tags,
    remembered_artist,
    save_episode_tags,
)
from tests.test_chapters import _make_silent_mp3, _make_test_jpeg
from tests.test_web_editor import _ctx, running_server

SET = "2026-09-23-Keys-Lounge_tracklist.md"


@pytest.fixture
def mp3(tmp_path):
    return _make_silent_mp3(tmp_path / "set.mp3", duration_seconds=300.0)


TRACKS = [
    Track(timestamp=0, artist="Daft Punk", title="Around the World"),
    Track(timestamp=90, artist="The Chemical Brothers", title="Block Rockin' Beats"),
    Track(timestamp=210, artist="Fatboy Slim", title="Praise You"),
]


# --- the module ------------------------------------------------------------


def test_the_file_sits_beside_the_tracklist(tmp_path):
    assert episode_path_for(tmp_path / SET) == tmp_path / "2026-09-23-Keys-Lounge_episode.json"


@pytest.mark.parametrize(
    "name, title",
    [
        (SET, "September 23, 2026"),
        ("2026-01-05_tracklist.md", "January 5, 2026"),
        ("Keys-Lounge_tracklist.md", ""),
        ("2026-13-40-Nope_tracklist.md", ""),
        ("20260923-set_tracklist.md", ""),
    ],
)
def test_default_title_is_the_date_the_set_is_named_for(tmp_path, name, title):
    assert default_title(tmp_path / name) == title


def test_clean_tag_is_one_trimmed_capped_line():
    assert clean_tag("  DJ \n Disarray\t") == "DJ Disarray"
    assert clean_tag(None) == "" and clean_tag(42) == ""
    assert len(clean_tag("x" * 500)) == 200


def test_a_new_set_gets_the_defaults(tmp_path):
    assert episode_tags(tmp_path / SET) == {"title": "September 23, 2026", "artist": ""}


def test_saving_stores_the_set_and_remembers_the_artist(tmp_path):
    md = tmp_path / SET
    save_episode_tags(md, " Keys Lounge ", "DJ Disarray")

    assert episode_tags(md) == {"title": "Keys Lounge", "artist": "DJ Disarray"}
    assert remembered_artist() == "DJ Disarray"
    # The next set starts with that artist and its own date.
    other = tmp_path / "2026-10-01-Next_tracklist.md"
    assert episode_tags(other) == {"title": "October 1, 2026", "artist": "DJ Disarray"}


def test_a_saved_blank_stays_blank_and_is_not_remembered(tmp_path):
    md = tmp_path / SET
    save_episode_tags(tmp_path / "2026-01-01-a_tracklist.md", "", "DJ Disarray")
    save_episode_tags(md, "", "")

    assert episode_tags(md) == {"title": "", "artist": ""}
    assert remembered_artist() == "DJ Disarray"


def test_remembering_keeps_the_rest_of_the_config(tmp_path):
    config_path().parent.mkdir(parents=True)
    config_path().write_text(json.dumps({"other": 1}))
    save_episode_tags(tmp_path / SET, "T", "DJ Disarray")
    assert json.loads(config_path().read_text()) == {"other": 1, "episode_artist": "DJ Disarray"}


@pytest.mark.parametrize("junk", ["not json", "[1, 2]", '{"title": 5}'])
def test_a_hand_edited_file_degrades_to_defaults(tmp_path, junk):
    md = tmp_path / SET
    episode_path_for(md).write_text(junk)
    tags = episode_tags(md)
    assert tags["artist"] == ""
    assert tags["title"] in ("", "September 23, 2026")


# --- the MP3 ---------------------------------------------------------------


def test_embed_writes_title_album_and_artist(mp3):
    embed_chapters(mp3, TRACKS, title="September 23, 2026", artist="DJ Disarray")
    tags = MP3(str(mp3)).tags
    assert str(tags["TIT2"]) == "September 23, 2026"
    assert str(tags["TALB"]) == "September 23, 2026"
    assert str(tags["TPE1"]) == "DJ Disarray"
    # The chapters' own titles are sub-frames, untouched by the episode's.
    assert [str(c.sub_frames["TIT2"]) for c in tags.getall("CHAP")][0] == (
        "Daft Punk - Around the World"
    )


def test_embed_without_them_leaves_the_recorders_tags(mp3):
    audio = MP3(str(mp3))
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=["Recorder title"]))
    audio.tags.add(TPE1(encoding=3, text=["Recorder artist"]))
    audio.save(v2_version=3)

    embed_chapters(mp3, TRACKS, title="", artist=None)
    tags = MP3(str(mp3)).tags
    assert str(tags["TIT2"]) == "Recorder title"
    assert str(tags["TPE1"]) == "Recorder artist"
    assert "TALB" not in tags

    embed_chapters(mp3, TRACKS, title="New", artist="DJ Disarray")
    tags = MP3(str(mp3)).tags
    assert len(tags.getall("TIT2")) == 1 and str(tags["TIT2"]) == "New"
    assert str(tags["TPE1"]) == "DJ Disarray"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_ffprobe_reads_the_tags_and_the_chapters_in_order(mp3):
    """Extra top-level frames change the layout the chapter reordering walks."""
    images = {i: _make_test_jpeg(300 - 100 * i) for i in range(len(TRACKS))}
    embed_chapters(
        mp3, TRACKS, chapter_images=images, title="September 23, 2026", artist="DJ Disarray"
    )
    out = subprocess.run(
        [
            "ffprobe",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_chapters",
            "-show_format",
            str(mp3),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(out.stdout)
    tags = data["format"]["tags"]
    assert (tags["title"], tags["album"], tags["artist"]) == (
        "September 23, 2026",
        "September 23, 2026",
        "DJ Disarray",
    )
    assert [int(c["start"]) for c in data["chapters"]] == [0, 90_000, 210_000]


def test_chapters_for_a_tracklist_uses_the_saved_or_default_tags(
    sample_tracklist, tmp_path, monkeypatch
):
    from setlist_maker.cli import embed_chapters_for_tracklist

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters", lambda **kw: embedded.update(kw) or kw["audio_path"]
    )
    md = tmp_path / SET
    embed_chapters_for_tracklist(
        sample_tracklist, tmp_path / "s.mp3", fetch_art=False, tracklist_path=md
    )
    assert (embedded["title"], embedded["artist"]) == ("September 23, 2026", "")

    save_episode_tags(md, "Keys Lounge", "DJ Disarray")
    embed_chapters_for_tracklist(
        sample_tracklist, tmp_path / "s.mp3", fetch_art=False, tracklist_path=md
    )
    assert (embedded["title"], embedded["artist"]) == ("Keys Lounge", "DJ Disarray")


# --- the server ------------------------------------------------------------


def _post_save(base, payload):
    req = urllib.request.Request(
        base + "/api/save", method="POST", data=json.dumps(payload).encode()
    )
    req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req)


def test_the_page_gets_and_saves_the_episode(sample_tracklist, tmp_path):
    ctx = _ctx(sample_tracklist, tmp_path)
    ctx.output_path = tmp_path / SET
    save_episode_tags(tmp_path / "2026-01-01-a_tracklist.md", "", "DJ Disarray")
    with running_server(ctx) as base:
        with urllib.request.urlopen(base + "/api/tracklist") as r:
            assert json.loads(r.read())["episode"] == {
                "title": "September 23, 2026",
                "artist": "DJ Disarray",
            }
        with _post_save(base, {"tracks": [], "episode": {"title": "Keys", "artist": "Me"}}) as r:
            assert json.loads(r.read())["ok"]
        with urllib.request.urlopen(base + "/api/tracklist") as r:
            assert json.loads(r.read())["episode"] == {"title": "Keys", "artist": "Me"}
    assert remembered_artist() == "Me"


def test_a_save_without_the_episode_leaves_it_alone(sample_tracklist, tmp_path):
    ctx = _ctx(sample_tracklist, tmp_path)
    ctx.output_path = tmp_path / SET
    with running_server(ctx) as base:
        with _post_save(base, {"tracks": []}) as r:
            assert json.loads(r.read())["ok"]
    assert not episode_path_for(ctx.output_path).exists()


def test_a_malformed_episode_saves_nothing(sample_tracklist, tmp_path):
    ctx = _ctx(sample_tracklist, tmp_path)
    ctx.output_path = tmp_path / SET
    before = ctx.output_path.read_text() if ctx.output_path.exists() else None
    with running_server(ctx) as base:
        with pytest.raises(urllib.error.HTTPError):
            _post_save(base, {"tracks": [], "summary": "changed", "episode": "Keys"})
    after = ctx.output_path.read_text() if ctx.output_path.exists() else None
    assert before == after
    assert not episode_path_for(ctx.output_path).exists()
