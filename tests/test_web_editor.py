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
