"""Tests for the chapter-artwork cache (setlist_maker.artwork_cache)."""

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    """Redirect the cache into tmp_path so tests never touch the real ~/.cache.

    Also clears ``_key_locks`` before and after every test. Other test files
    reuse the same ("Daft Punk", "Around the World") key; without this, state
    leaked from one test could cause order-dependent failures in another file.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from setlist_maker import artwork_cache

    artwork_cache._key_locks.clear()
    yield
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


def _real_jpeg(color=(10, 120, 90)):
    """A real decodable JPEG, for the paths that need source art to load."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (600, 600), color).save(buf, format="JPEG")
    return buf.getvalue()


def _fake_art(monkeypatch, returns=None):
    """Patch the network fetch; create_chapter_image stays real (Pillow, offline).

    Returns the call log. ``returns=None`` (the default) simulates a lookup that
    found nothing, which composites to the gradient fallback.
    """
    calls = []

    def fake_fetch(artist, title, coverart_url=None, size=600):
        calls.append((artist, title, coverart_url))
        return returns

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
    path = cache_dir() / f"{key}.jpg"
    path.write_bytes(b"truncated garbage")

    data = chapter_image("Daft Punk", "Around the World")
    assert data.startswith(b"\xff\xd8")  # regenerated rather than served corrupt
    assert path.read_bytes() == data  # and the corrupt file was replaced
    # No re-fetch: this key's empty lookup is already cached, so regenerating
    # the composite costs nothing on the network.
    assert len(calls) == 1


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
    path = cache_dir() / f"{key}.jpg"
    path.write_bytes(b"\xff\xd8" + b"x" * 100)  # valid SOI, no EOI marker

    data = chapter_image("Daft Punk", "Around the World")
    assert data.endswith(b"\xff\xd9")  # regenerated rather than served truncated
    assert path.read_bytes() == data  # and the truncated file was replaced
    assert len(calls) == 1  # empty lookup already cached; no re-fetch needed


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


def test_source_artwork_caches_the_bytes_it_found(monkeypatch):
    from setlist_maker.artwork_cache import cache_dir, cache_key, source_artwork

    real_art = _real_jpeg()
    calls = _fake_art(monkeypatch, returns=real_art)

    assert source_artwork("Daft Punk", "Around the World") == real_art
    assert source_artwork("Daft Punk", "Around the World") == real_art
    assert len(calls) == 1  # second call served from .src

    key = cache_key("Daft Punk", "Around the World", None, 600)
    assert (cache_dir() / f"{key}.src").read_bytes() == real_art
    assert not (cache_dir() / f"{key}.fallback").exists()


def test_an_empty_lookup_is_cached_and_not_retried(monkeypatch):
    """The marker is a negative-result cache: no art means don't look again.

    Without it, a track with nothing findable re-runs the six-request waterfall
    on every single run, which is the whole reason the marker exists.
    """
    from setlist_maker.artwork_cache import cache_dir, cache_key, source_artwork

    calls = _fake_art(monkeypatch)  # fetch returns None

    assert source_artwork("Daft Punk", "Around the World") is None
    assert source_artwork("Daft Punk", "Around the World") is None
    assert len(calls) == 1  # the second call never re-fetched

    key = cache_key("Daft Punk", "Around the World", None, 600)
    assert (cache_dir() / f"{key}.fallback").exists()
    assert not (cache_dir() / f"{key}.src").exists()


def test_the_empty_result_survives_a_fresh_process(monkeypatch):
    """The marker is a file, so `chapters` after an editing session still knows.

    Nothing in-process is consulted, so simply reaching for the same key from a
    cold module state must still short-circuit.
    """
    from setlist_maker.artwork_cache import source_artwork

    calls = _fake_art(monkeypatch)
    source_artwork("Daft Punk", "Around the World")

    # A fresh process shares only the cache directory -- no module state.
    assert source_artwork("Daft Punk", "Around the World") is None
    assert len(calls) == 1


def test_cached_source_bytes_win_over_a_stale_marker(monkeypatch):
    """If both somehow exist, the real artwork is the better answer."""
    from setlist_maker.artwork_cache import cache_dir, cache_key, source_artwork

    real_art = _real_jpeg()
    _fake_art(monkeypatch, returns=real_art)
    source_artwork("Daft Punk", "Around the World")

    key = cache_key("Daft Punk", "Around the World", None, 600)
    (cache_dir() / f"{key}.fallback").touch()  # stale marker alongside real art

    assert source_artwork("Daft Punk", "Around the World") == real_art


def test_missing_src_marks_the_key_and_leaves_the_composite_alone(monkeypatch):
    """A cached .jpg whose .src is gone: a failed re-fetch is recorded as empty.

    This is a deliberate behavior change from the previous design, which
    refused to mark a key whose composite was already cached. That guard
    existed because the flag then claimed something about the *composite* --
    marking it would have wrongly said "this track's chapter image is a
    gradient". Now the marker only claims the *source lookup* found nothing,
    which is exactly true here, so recording it is right and it stops the
    waterfall re-running on every future run.

    The consequences that matter both hold: the cached composite still shows
    the real art, and the episode cover skips this track rather than latching
    a gradient (asserted in tests/test_cli.py).
    """
    from setlist_maker.artwork_cache import cache_dir, cache_key, chapter_image, source_artwork

    real_art = _real_jpeg()
    _fake_art(monkeypatch, returns=real_art)
    composite = chapter_image("Daft Punk", "Around the World")

    key = cache_key("Daft Punk", "Around the World", None, 600)
    (cache_dir() / f"{key}.src").unlink()  # .jpg survives, .src does not

    calls = _fake_art(monkeypatch)  # the re-fetch now fails
    assert source_artwork("Daft Punk", "Around the World") is None
    assert len(calls) == 1
    assert (cache_dir() / f"{key}.fallback").exists()

    # The composite is untouched: still the real art, still served from cache.
    assert chapter_image("Daft Punk", "Around the World") == composite
    assert len(calls) == 1  # .jpg hit; no further fetching


def test_unreadable_cache_dir_does_not_crash_source_artwork(monkeypatch):
    """An unreadable cache must degrade to a plain fetch, not raise.

    Regression: the marker probe uses Path.exists(), which re-raises
    PermissionError (EACCES) rather than swallowing it like the module's other
    filesystem touches. An unreadable cache dir must not take down `chapters`.
    """
    import os

    from setlist_maker import artwork_cache

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root: chmod restrictions have no effect")

    _fake_art(monkeypatch)

    cache_root = artwork_cache.cache_dir()
    cache_root.mkdir(parents=True)
    cache_root.chmod(0o000)  # unreadable and untraversable

    try:
        assert artwork_cache.source_artwork("Daft Punk", "Around the World") is None
        assert artwork_cache.chapter_image("Daft Punk", "Around the World").startswith(b"\xff\xd8")
    finally:
        cache_root.chmod(0o755)  # restore so tmp_path cleanup can remove it


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


def test_corrected_track_searches_on_new_metadata_not_the_stale_url(monkeypatch):
    """End-to-end guard for #30, spanning the editor's edit step and the lookup.

    ``fetch_artwork()`` tries ``coverart_url`` ahead of every search, so a
    correction that left the URL attached kept re-downloading the misidentified
    track's cover -- regenerating (artist and title are in the cache key) but
    regenerating the same wrong picture.
    """
    from setlist_maker.artwork_cache import source_artwork
    from setlist_maker.editor import Track, apply_track_edit

    track = Track(
        timestamp=0,
        artist="Wrong Artist",
        title="Wrong Title",
        coverart_url="https://cdn.shazam.com/wrong-album.jpg",
    )
    apply_track_edit(track, "Justice", "Genesis")

    calls = _fake_art(monkeypatch)
    source_artwork(track.artist, track.title, track.coverart_url)

    assert calls == [("Justice", "Genesis", None)]
