"""Tests for the chapter-artwork cache (setlist_maker.artwork_cache)."""

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    """Redirect the cache into tmp_path so tests never touch the real ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def test_cache_dir_honors_xdg(tmp_path):
    from setlist_maker.artwork_cache import cache_dir

    assert cache_dir() == tmp_path / "cache" / "setlist-maker" / "artwork"


def test_cache_dir_falls_back_to_home(monkeypatch, tmp_path):
    from setlist_maker.artwork_cache import cache_dir

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cache_dir() == tmp_path / "home" / ".cache" / "setlist-maker" / "artwork"


def test_cache_key_is_stable():
    from setlist_maker.artwork_cache import cache_key

    a = cache_key("Daft Punk", "Around the World", "http://x/y.jpg", 600)
    b = cache_key("Daft Punk", "Around the World", "http://x/y.jpg", 600)
    assert a == b
    assert len(a) == 64


@pytest.mark.parametrize(
    "args",
    [
        ("Other Artist", "Around the World", "http://x/y.jpg", 600),
        ("Daft Punk", "Other Title", "http://x/y.jpg", 600),
        ("Daft Punk", "Around the World", "http://x/other.jpg", 600),
        ("Daft Punk", "Around the World", None, 600),
        ("Daft Punk", "Around the World", "http://x/y.jpg", 300),
    ],
)
def test_cache_key_changes_with_every_input(args):
    """Each of artist, title, coverart_url and size must affect the key."""
    from setlist_maker.artwork_cache import cache_key

    base = cache_key("Daft Punk", "Around the World", "http://x/y.jpg", 600)
    assert cache_key(*args) != base


def test_cache_key_separator_is_unambiguous():
    """Field boundaries must not collide: ("ab","c") and ("a","bc") differ."""
    from setlist_maker.artwork_cache import cache_key

    assert cache_key("ab", "c", None, 600) != cache_key("a", "bc", None, 600)
