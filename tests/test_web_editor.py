"""Tests for the web editor (setlist_maker.web_editor)."""

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
