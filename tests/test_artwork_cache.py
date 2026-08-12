"""Tests for the chapter-artwork cache (setlist_maker.artwork_cache)."""

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    """Redirect the cache into tmp_path so tests never touch the real ~/.cache.

    Also clears module-level state (``_fallback_seen``, ``_key_locks``) before
    and after every test. Other test files (Tasks 3 and 5) reuse the same
    ("Daft Punk", "Around the World") key; without this, state leaked from one
    test would cause order-dependent failures in another file.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from setlist_maker import artwork_cache

    artwork_cache._fallback_seen.clear()
    artwork_cache._key_locks.clear()
    yield
    artwork_cache._fallback_seen.clear()
    artwork_cache._key_locks.clear()


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


def _fake_art(monkeypatch, data=b"\xff\xd8fake-jpeg"):
    """Patch the network fetch; create_chapter_image stays real (Pillow, offline)."""
    calls = []

    def fake_fetch(artist, title, coverart_url=None, size=600):
        calls.append((artist, title, coverart_url))
        return None  # -> gradient fallback composite, no network

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", fake_fetch)
    return calls


def test_chapter_image_returns_jpeg_bytes(monkeypatch):
    from setlist_maker.artwork_cache import chapter_image

    _fake_art(monkeypatch)
    data = chapter_image("Daft Punk", "Around the World")
    assert data.startswith(b"\xff\xd8")  # JPEG SOI marker


def test_chapter_image_writes_the_bytes_it_returns(monkeypatch):
    from setlist_maker.artwork_cache import cache_dir, cache_key, chapter_image

    _fake_art(monkeypatch)
    data = chapter_image("Daft Punk", "Around the World")

    key = cache_key("Daft Punk", "Around the World", None, 600)
    assert (cache_dir() / f"{key}.jpg").read_bytes() == data


def test_second_call_is_a_hit_and_skips_the_network(monkeypatch):
    from setlist_maker.artwork_cache import chapter_image

    calls = _fake_art(monkeypatch)
    first = chapter_image("Daft Punk", "Around the World")
    second = chapter_image("Daft Punk", "Around the World")

    assert first == second
    assert len(calls) == 1  # the second call never re-fetched


def test_edited_title_regenerates(monkeypatch):
    from setlist_maker.artwork_cache import chapter_image

    calls = _fake_art(monkeypatch)
    chapter_image("Daft Punk", "Around the World")
    chapter_image("Daft Punk", "Around the Word")  # user fixed a typo

    assert len(calls) == 2


def test_corrupt_cache_file_is_regenerated(monkeypatch):
    from setlist_maker.artwork_cache import cache_dir, cache_key, chapter_image

    calls = _fake_art(monkeypatch)
    chapter_image("Daft Punk", "Around the World")

    key = cache_key("Daft Punk", "Around the World", None, 600)
    (cache_dir() / f"{key}.jpg").write_bytes(b"truncated garbage")

    data = chapter_image("Daft Punk", "Around the World")
    assert data.startswith(b"\xff\xd8")
    assert len(calls) == 2  # regenerated rather than served corrupt


def test_truncated_cache_file_is_regenerated(monkeypatch):
    """A file with an intact JPEG header but a lost tail must not be served.

    Distinct from test_corrupt_cache_file_is_regenerated: that test's garbage
    fails the SOI-prefix check and never exercises the EOI-suffix check. This
    one has a valid SOI prefix but no EOI suffix, so it only fails validation
    via the `endswith(b"\xff\xd9")` half of _read_cached.
    """
    from setlist_maker.artwork_cache import cache_dir, cache_key, chapter_image

    calls = _fake_art(monkeypatch)
    chapter_image("Daft Punk", "Around the World")

    key = cache_key("Daft Punk", "Around the World", None, 600)
    (cache_dir() / f"{key}.jpg").write_bytes(b"\xff\xd8" + b"x" * 100)  # no EOI marker

    data = chapter_image("Daft Punk", "Around the World")
    assert data.startswith(b"\xff\xd8")
    assert len(calls) == 2  # regenerated rather than served the truncated file


def test_unwritable_cache_dir_still_returns_bytes(monkeypatch, tmp_path):
    """An unusable cache must degrade to in-memory generation, not fail.

    The cache dir is pointed *inside a regular file*, so mkdir raises
    NotADirectoryError for real -- no patching of pathlib internals.
    """
    from setlist_maker import artwork_cache

    _fake_art(monkeypatch)
    not_a_dir = tmp_path / "regular-file"
    not_a_dir.write_text("x")
    monkeypatch.setattr(artwork_cache, "cache_dir", lambda: not_a_dir / "artwork")

    data = artwork_cache.chapter_image("Daft Punk", "Around the World")
    assert data.startswith(b"\xff\xd8")
    assert not (not_a_dir / "artwork").exists()


def test_used_fallback_is_true_when_no_artwork_found(monkeypatch):
    from setlist_maker.artwork_cache import chapter_image, used_fallback

    _fake_art(monkeypatch)  # fetch returns None -> gradient fallback
    chapter_image("Daft Punk", "Around the World")
    assert used_fallback("Daft Punk", "Around the World") is True


def test_used_fallback_is_false_when_artwork_found(monkeypatch):
    import io

    from PIL import Image

    from setlist_maker.artwork_cache import chapter_image, used_fallback

    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (10, 120, 90)).save(buf, format="JPEG")
    real_art = buf.getvalue()
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: real_art,
    )

    chapter_image("Daft Punk", "Around the World")
    assert used_fallback("Daft Punk", "Around the World") is False


def test_used_fallback_survives_a_fresh_process(monkeypatch):
    """A cross-process cache hit must still know it was a fallback.

    This is the case that matters: the editor generates, then `chapters`
    runs later and must not pick a gradient as the episode cover.
    """
    from setlist_maker import artwork_cache

    _fake_art(monkeypatch)
    artwork_cache.chapter_image("Daft Punk", "Around the World")

    artwork_cache._fallback_seen.clear()  # simulate a new process
    assert artwork_cache.used_fallback("Daft Punk", "Around the World") is True


def test_missing_src_after_cached_composite_does_not_poison_used_fallback(monkeypatch):
    """A cached composite with no .src must not be marked as fallback.

    Regression: a .jpg can exist without its .src (an older build, a pruned
    .src, or an unwritable cache when it was first generated). If the episode
    cover then calls source_artwork() for that key and the re-fetch fails
    (rate limit, network blip, artwork taken down), that failure must not be
    recorded as this key's fallback status -- doing so would permanently and
    silently swap that track's episode cover for the gradient, even though
    its own cached chapter image still shows the real art.
    """
    import io

    from PIL import Image

    from setlist_maker.artwork_cache import (
        cache_dir,
        cache_key,
        chapter_image,
        source_artwork,
        used_fallback,
    )

    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (10, 120, 90)).save(buf, format="JPEG")
    real_art = buf.getvalue()
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: real_art,
    )

    chapter_image("Daft Punk", "Around the World")
    assert used_fallback("Daft Punk", "Around the World") is False

    # Simulate a .jpg cached without its .src.
    key = cache_key("Daft Punk", "Around the World", None, 600)
    (cache_dir() / f"{key}.src").unlink()

    # A later re-fetch attempt for the same key fails.
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: None,
    )
    assert source_artwork("Daft Punk", "Around the World") is None  # the fetch did fail

    assert used_fallback("Daft Punk", "Around the World") is False
    assert not (cache_dir() / f"{key}.fallback").exists()


def test_concurrent_calls_generate_once(monkeypatch):
    """Two threads racing on one key must not both hit the network."""
    import threading

    from setlist_maker.artwork_cache import chapter_image

    calls = _fake_art(monkeypatch)
    results = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(chapter_image("Daft Punk", "Around the World"))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 2
    assert results[0] == results[1]
    assert len(calls) == 1
