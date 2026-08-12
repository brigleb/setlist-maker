# Chapter Artwork Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the web editor's row thumbnail show the actual `create_chapter_image()` composite that will be embedded, clickable to full size, backed by a cache that the `chapters` path also reads so the approved bytes are the shipped bytes.

**Architecture:** A new `setlist_maker/artwork_cache.py` owns one cached function, `chapter_image()`, which wraps the existing *fetch → composite* pair. Both the new `GET /api/artwork` endpoint in `web_editor.py` and the existing `embed_chapters_for_tracklist()` in `cli.py` call it, so only one code path can produce a chapter image. The cache key is a content hash, so edits invalidate structurally and there is no invalidation logic to get wrong.

**Tech Stack:** Python 3.10+, stdlib (`hashlib`, `threading`, `os`, `http.server`), Pillow (already a dependency, via `artwork.py`), vanilla JS `IntersectionObserver`, pytest.

## Global Constraints

- Python 3.10+; line length 100 (`pyproject.toml`); ruff rules E, F, W, I.
- Cache location: `$XDG_CACHE_HOME/setlist-maker/artwork`, else `~/.cache/setlist-maker/artwork`.
- `cache_dir()` must read the environment on **every call** so tests can redirect it with `monkeypatch.setenv("XDG_CACHE_HOME", ...)`. Do not compute it at import time.
- No test may touch the network or the real `~/.cache`. Patch `setlist_maker.artwork_cache.fetch_artwork` and set `XDG_CACHE_HOME` to a `tmp_path`.
- `web_editor.html` keeps its no-`innerHTML` posture: never interpolate track text into markup. Image URLs carry an integer index only.
- All 238 existing tests must stay green.

---

## File Structure

- **Create `setlist_maker/artwork_cache.py`** — `cache_dir()`, `cache_key()`, `chapter_image()`. Caching only; fetching stays in `artwork.py`, drawing stays in `artwork.py`.
- **Create `tests/test_artwork_cache.py`** — unit tests for the above.
- **Modify `setlist_maker/web_editor.py`** — add `GET /api/artwork?index=N` handling.
- **Modify `tests/test_web_editor.py`** — endpoint tests against the live server.
- **Modify `setlist_maker/web_editor.html`** — lazy composite thumbs, click overlay, re-render after save.
- **Modify `setlist_maker/cli.py:283-317`** — `embed_chapters_for_tracklist()` calls `chapter_image()`.
- **Modify `tests/test_cli.py`** — assert the chapters path reuses the cache.
- **Modify `CLAUDE.md`** — document the new module and the changed chapters behavior.
- **Modify `README.md:103`** — note that previewed artwork is reused by `chapters`.

---

### Task 1: Cache location and key

**Files:**
- Create: `setlist_maker/artwork_cache.py`
- Test: `tests/test_artwork_cache.py`

**Interfaces:**
- Consumes: `CHAPTER_IMAGE_SIZE` from `setlist_maker.artwork`
- Produces: `cache_dir() -> Path`, `cache_key(artist: str, title: str, coverart_url: str | None, size: int) -> str` (64-char sha256 hex)

- [ ] **Step 1: Write the failing test**

Create `tests/test_artwork_cache.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_artwork_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'setlist_maker.artwork_cache'`

- [ ] **Step 3: Write minimal implementation**

Create `setlist_maker/artwork_cache.py`:

```python
"""Disk cache for generated chapter images.

The web editor previews the same composite that ``chapters`` embeds, so both
call ``chapter_image()`` here rather than pairing ``fetch_artwork()`` with
``create_chapter_image()`` themselves. Caching the result makes the preview
authoritative: what the user approved in the editor is what gets written into
the MP3, and a post-editing ``--chapters`` run costs no network.
"""

import hashlib
import os
from pathlib import Path

from setlist_maker.artwork import CHAPTER_IMAGE_SIZE

# Field separator for the hashed key. A NUL byte cannot appear in artist,
# title or URL text, so field boundaries can never be ambiguous.
_KEY_SEP = "\0"


def cache_dir() -> Path:
    """Return the artwork cache directory (not created).

    Reads the environment on every call so tests can redirect it.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "setlist-maker" / "artwork"


def cache_key(artist: str, title: str, coverart_url: str | None, size: int) -> str:
    """Hash everything that affects the rendered image.

    Because the key covers every input, an edited artist or title yields a
    different key and regenerates on its own -- there is no invalidation step
    to forget. ``size`` is included so changing CHAPTER_IMAGE_SIZE invalidates
    rather than serving mis-sized art.
    """
    raw = _KEY_SEP.join([artist, title, coverart_url or "", str(size)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_artwork_cache.py -v`
Expected: PASS (9 tests — the parametrized key test contributes 5)

- [ ] **Step 5: Lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: "All checks passed!"

- [ ] **Step 6: Commit**

```bash
git add setlist_maker/artwork_cache.py tests/test_artwork_cache.py
git commit -m "feat(artwork-cache): add cache location and content-hash key"
```

---

### Task 2: Cached `chapter_image()`

**Files:**
- Modify: `setlist_maker/artwork_cache.py`
- Test: `tests/test_artwork_cache.py`

**Interfaces:**
- Consumes: `cache_dir()`, `cache_key()` from Task 1; `fetch_artwork`, `create_chapter_image`, `CHAPTER_IMAGE_SIZE` from `setlist_maker.artwork`
- Produces:
  - `chapter_image(artist: str, title: str, coverart_url: str | None = None, size: int = CHAPTER_IMAGE_SIZE) -> bytes` — always returns JPEG bytes, never raises for a missing-artwork or unwritable-cache condition
  - `used_fallback(artist: str, title: str, coverart_url: str | None = None, size: int = CHAPTER_IMAGE_SIZE) -> bool` — True when the composite for that key was drawn on the gradient because no source artwork was found. Only meaningful after `chapter_image()` has been called for the same key; Task 5 relies on it to keep the episode cover on the first track with *real* art.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_artwork_cache.py`:

```python
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
    from PIL import Image

    from setlist_maker.artwork_cache import chapter_image, used_fallback

    import io

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_artwork_cache.py -v`
Expected: FAIL — `ImportError: cannot import name 'chapter_image'`

- [ ] **Step 3: Write minimal implementation**

Add to the imports at the top of `setlist_maker/artwork_cache.py`:

```python
import logging
import tempfile
import threading

from setlist_maker.artwork import CHAPTER_IMAGE_SIZE, create_chapter_image, fetch_artwork

logger = logging.getLogger(__name__)

# Cap simultaneous generation. Each miss can make up to six network calls, so
# an editor scrolling 60 rows must not open 60 fetch storms. Cache hits never
# take the semaphore -- only generation is capped.
_MAX_CONCURRENT_GENERATION = 4
_generation_slots = threading.Semaphore(_MAX_CONCURRENT_GENERATION)

# One lock per cache key so two requests for the same track generate once.
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()

# Keys whose composite was drawn on the gradient because no source artwork was
# found. Mirrored to a marker file so a later process (the `chapters` run after
# an editing session) still knows, and does not pick a gradient as the episode
# cover. The in-process set also covers the case where the cache is unwritable.
_fallback_seen: set[str] = set()
```

Then append:

```python
def _lock_for(key: str) -> threading.Lock:
    """Return the (created-on-demand) lock guarding one cache key."""
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _read_cached(path: Path) -> bytes | None:
    """Return cached JPEG bytes, or None if absent/unreadable/not a JPEG."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    # A truncated or clobbered file must not be served as artwork.
    return data if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9") else None


def _write_cached(path: Path, data: bytes) -> None:
    """Write atomically, or give up quietly if the cache isn't writable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)  # atomic: readers never see a partial file
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        logger.debug("Artwork cache write skipped for %s: %s", path, e)


def chapter_image(
    artist: str,
    title: str,
    coverart_url: str | None = None,
    size: int = CHAPTER_IMAGE_SIZE,
) -> bytes:
    """Return the chapter composite for a track, generating it only on a miss.

    Always returns JPEG bytes: when no artwork is found anywhere,
    ``create_chapter_image`` renders its gradient fallback, which is exactly
    what would be embedded, so it is cached like any other result.
    """
    key = cache_key(artist, title, coverart_url, size)
    path = cache_dir() / f"{key}.jpg"

    cached = _read_cached(path)
    if cached is not None:
        return cached

    with _lock_for(key):
        # Another thread may have generated this while we waited for the lock.
        cached = _read_cached(path)
        if cached is not None:
            return cached

        with _generation_slots:
            artwork_bytes = fetch_artwork(
                artist=artist, title=title, coverart_url=coverart_url, size=size
            )
            data = create_chapter_image(
                artwork_bytes=artwork_bytes, artist=artist, title=title, size=size
            )

        # Record fallback-ness BEFORE writing the image. If this crashed after
        # the write instead, the .jpg would exist with no .fallback marker, and
        # every later call would hit the cache and return before reaching this
        # line -- used_fallback() would report False for a gradient forever.
        # This order fails safe: a crash leaves no image, so the next call
        # regenerates and re-records.
        _record_fallback(key, artwork_bytes is None)
        _write_cached(path, data)
        return data


def _fallback_marker(key: str) -> Path:
    return cache_dir() / f"{key}.fallback"


def _record_fallback(key: str, is_fallback: bool) -> None:
    """Persist whether this composite was drawn without real source artwork."""
    marker = _fallback_marker(key)
    if is_fallback:
        _fallback_seen.add(key)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass  # in-process set still answers for this run
    else:
        _fallback_seen.discard(key)
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def used_fallback(
    artist: str,
    title: str,
    coverart_url: str | None = None,
    size: int = CHAPTER_IMAGE_SIZE,
) -> bool:
    """True if this track's composite had no real artwork behind it.

    Lets the episode cover keep skipping artless tracks the way it did before
    compositing moved behind this cache.
    """
    key = cache_key(artist, title, coverart_url, size)
    return key in _fallback_seen or _fallback_marker(key).exists()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_artwork_cache.py -v`
Expected: PASS (19 tests)

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: 257 passed (238 + 19); "All checks passed!"

- [ ] **Step 6: Update CLAUDE.md**

In the module list, after the `chapters.py` + `artwork.py` section, add:

```markdown
### `setlist_maker/artwork_cache.py` - Chapter image cache
- **chapter_image():** The single path from a track to its chapter composite —
  `fetch_artwork()` + `create_chapter_image()`, cached on disk. Called by both the
  web editor's `/api/artwork` preview and `embed_chapters_for_tracklist()`, so the
  image previewed is byte-identical to the one embedded (#20).
- **Cache key is a content hash** of (artist, title, coverart_url, size), so an edit
  regenerates structurally — there is no invalidation code path. Lives in
  `$XDG_CACHE_HOME/setlist-maker/artwork` (else `~/.cache/...`).
- Per-key locks dedupe concurrent requests; a semaphore caps simultaneous generation
  at 4. An unwritable cache degrades to in-memory generation rather than failing.
```

- [ ] **Step 7: Commit**

```bash
git add setlist_maker/artwork_cache.py tests/test_artwork_cache.py CLAUDE.md
git commit -m "feat(artwork-cache): cache chapter composites on disk"
```

---

### Task 3: `GET /api/artwork` endpoint

**Files:**
- Modify: `setlist_maker/web_editor.py`
- Test: `tests/test_web_editor.py`

**Interfaces:**
- Consumes: `chapter_image()` from Task 2; existing `EditorContext`, `_Handler`, `create_server`
- Produces: `GET /api/artwork?index=N` → `200 image/jpeg` | `404`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_editor.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_editor.py -k artwork -v`
Expected: FAIL — HTTP 404 on the first test (route not implemented)

- [ ] **Step 3: Write minimal implementation**

In `setlist_maker/web_editor.py`, add to the imports:

```python
from urllib.parse import parse_qs, urlparse

from setlist_maker.artwork_cache import chapter_image
```

(The existing line is `from urllib.parse import urlparse` — extend it as shown.)

In `_Handler.do_GET`, add a branch before the trailing `else`:

```python
        elif path == "/api/artwork":
            self._send_artwork()
```

Add the method to `_Handler`:

```python
    def _send_artwork(self) -> None:
        """Serve the chapter composite for one track, generating it on demand.

        Index-based rather than artist/title-from-the-page on purpose: the
        cache is authoritative, so the preview must reflect *saved* state --
        that is what ``chapters`` will embed.
        """
        params = parse_qs(urlparse(self.path).query)
        try:
            index = int(params.get("index", [""])[0])
        except (TypeError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND, "bad index")
            return

        tracks = self._ctx.tracklist.tracks
        if not 0 <= index < len(tracks):
            self.send_error(HTTPStatus.NOT_FOUND, "no such track")
            return

        track = tracks[index]
        if track.is_unidentified:
            # chapters skips unidentified tracks, so there is nothing to preview
            self.send_error(HTTPStatus.NOT_FOUND, "track is unidentified")
            return

        data = chapter_image(
            artist=track.artist, title=track.title, coverart_url=track.coverart_url
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        # The page re-requests after a save; never serve a pre-edit composite.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # row scrolled away / page closed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_editor.py -k artwork -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: 264 passed (257 + 7); "All checks passed!"

- [ ] **Step 6: Commit**

```bash
git add setlist_maker/web_editor.py tests/test_web_editor.py
git commit -m "feat(web-edit): serve the real chapter composite at /api/artwork"
```

---

### Task 4: Composite thumbnails and full-size overlay

**Files:**
- Modify: `setlist_maker/web_editor.html`
- Test: `tests/test_web_editor.py`

**Interfaces:**
- Consumes: `GET /api/artwork?index=N` from Task 3
- Produces: no Python interface; the page's existing `rowEl(t, i)` gains lazy artwork loading

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_editor.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_editor.py -k lazy_loads -v`
Expected: FAIL — `assert '/api/artwork?index=' in html`

- [ ] **Step 3: Add the overlay markup and styles**

In `setlist_maker/web_editor.html`, add to the `<style>` block (next to the existing `.thumb` rule):

```css
  .thumb { cursor:zoom-in; }
  #artwork-overlay { position:fixed; inset:0; background:rgba(0,0,0,.72); display:none;
    align-items:center; justify-content:center; z-index:50; cursor:zoom-out; }
  #artwork-overlay.open { display:flex; }
  #artwork-overlay img { width:min(90vw,600px); height:auto; border-radius:10px;
    box-shadow:0 12px 48px rgba(0,0,0,.5); }
```

Immediately **before the opening `<script>` tag** (the script block runs at
parse time and looks this element up at top level, so the element must already
exist in the DOM — placing it after `</script>` makes `getElementById` return
`null` and the resulting TypeError kills the entire script, rendering a blank
editor):

```html
<div id="artwork-overlay"><img alt="Chapter artwork preview"></div>
```

- [ ] **Step 4: Wire up lazy loading and the overlay**

In the `<script>` block, add above `function rowEl(t, i) {`:

```javascript
  // Generate visible rows first: 60-track sets are network-bound on a miss.
  const artObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const el = e.target;
      artObserver.unobserve(el);
      const img = new Image();
      // 404 = unidentified or out of range; keep the gradient placeholder.
      img.onload = () => { el.style.backgroundImage = "url('" + img.src + "')"; };
      img.src = "/api/artwork?index=" + el.dataset.index;
    }
  }, { rootMargin: "200px" });  // start just before the row scrolls into view

  const overlay = document.getElementById("artwork-overlay");
  const overlayImg = overlay.querySelector("img");
  function showArtwork(index) {
    overlayImg.src = "/api/artwork?index=" + index;
    overlay.classList.add("open");
  }
  overlay.onclick = () => { overlay.classList.remove("open"); overlayImg.src = ""; };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay.classList.contains("open")) overlay.click();
  });
```

In `rowEl`, replace these two lines:

```javascript
    if (t.coverart_url) thumb.style.backgroundImage = "url('" + t.coverart_url + "')";
    else if (unknown) thumb.textContent = "?";
```

with:

```javascript
    if (unknown) {
      thumb.textContent = "?";
    } else {
      // The gradient stays until the composite lands, so rows never jump.
      thumb.dataset.index = t.index;
      thumb.onclick = (e) => { e.stopPropagation(); showArtwork(t.index); };
      artObserver.observe(thumb);
    }
```

- [ ] **Step 5: Re-render after save so edited rows refresh**

In the save handler, replace this line:

```javascript
        if (hasNew) await load();  // refresh so inserted rows gain real indices (re-save won't duplicate)
```

with:

```javascript
        // Always re-render: an edited artist/title changes the composite, and
        // the thumb must re-request against the now-saved server state.
        if (hasNew) await load();  // inserted rows also need real indices
        else render();
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_web_editor.py -k lazy_loads -v`
Expected: PASS

- [ ] **Step 7: Verify in a real browser**

```bash
uv run python - <<'PY'
from pathlib import Path
from setlist_maker.editor import Track, Tracklist
from setlist_maker.web_editor import run_web_editor
tl = Tracklist(source_file="demo.mp3", generated_on="2026-08-12 00:00", tracks=[
    Track(timestamp=0, artist="Daft Punk", title="Around the World"),
    Track(timestamp=180, artist="Justice", title="Genesis"),
    Track(timestamp=360, artist="", title=""),
])
run_web_editor(tl, Path("/tmp/demo_tracklist.md"), use_corrections=False)
PY
```

Confirm: thumbs fill in with lower-third composites (not raw covers), the unidentified row keeps its `?`, clicking a thumb opens the full-size overlay, and Escape closes it. Then press Ctrl-C.

- [ ] **Step 8: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: 265 passed (264 + 1); "All checks passed!"

- [ ] **Step 9: Commit**

```bash
git add setlist_maker/web_editor.html tests/test_web_editor.py
git commit -m "feat(web-edit): show the real chapter composite in row thumbnails"
```

---

### Task 5: Reuse the cache when embedding chapters

**Files:**
- Modify: `setlist_maker/cli.py:283-317`
- Test: `tests/test_cli.py`
- Modify: `CLAUDE.md`, `README.md:103`

**Interfaces:**
- Consumes: `chapter_image()` from Task 2
- Produces: no new interface; `embed_chapters_for_tracklist()` keeps its signature

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_embed_chapters_reuses_cached_artwork(monkeypatch, tmp_path, sample_tracklist):
    """A composite already generated in the editor is not re-fetched at embed time."""
    from setlist_maker import artwork_cache
    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: None,
    )

    # Warm the cache the way the editor's preview would.
    for t in sample_tracklist.tracks:
        if not t.is_unidentified:
            artwork_cache.chapter_image(t.artist, t.title, t.coverart_url)

    def explode(*a, **k):
        raise AssertionError("embed must reuse the cache, not re-fetch")

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", explode)

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    # three identified tracks in the fixture, each with a cached composite
    assert len(embedded["chapter_images"]) == 3
    # no track had real artwork, so there is no episode cover (as before the cache)
    assert embedded["episode_image"] is None


def _jpeg(color=(10, 120, 90)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (600, 600), color).save(buf, format="JPEG")
    return buf.getvalue()


def test_episode_cover_skips_a_track_with_no_artwork(monkeypatch, tmp_path, sample_tracklist):
    """The opener having no findable art must not yield a gradient episode cover.

    Pins the pre-cache behavior: the episode cover comes from the first track
    with *real* artwork, not merely the first identified one.
    """
    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def art_for_second_track_only(artist, title, coverart_url=None, size=600):
        return _jpeg() if artist == "The Chemical Brothers" else None

    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork", art_for_second_track_only
    )

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    assert embedded["episode_image"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -k reuses_cached -v`
Expected: FAIL — `AssertionError: embed must reuse the cache, not re-fetch` (and the episode-cover test fails on the gradient fall-through)

- [ ] **Step 3: Write minimal implementation**

In `setlist_maker/cli.py`, change the import on line 49 from:

```python
from setlist_maker.artwork import create_chapter_image, fetch_artwork
```

to:

```python
from setlist_maker.artwork_cache import chapter_image, used_fallback
```

In `embed_chapters_for_tracklist`, replace the body of the per-track loop (the `artwork_bytes = fetch_artwork(...)` block through `chapter_images[i] = chapter_img`, plus the episode-cover block) with:

```python
            # One cached path shared with the editor's preview, so the image
            # embedded here is byte-identical to the one the user approved.
            chapter_images[i] = chapter_image(
                artist=track.artist,
                title=track.title,
                coverart_url=track.coverart_url,
            )

            # Episode cover: first track with *real* artwork, relabelled for the
            # set. used_fallback() preserves the pre-cache behavior of skipping
            # tracks whose composite is just the gradient.
            if episode_image is None and not used_fallback(
                track.artist, track.title, track.coverart_url
            ):
                episode_image = chapter_image(
                    artist=tracklist.source_file.replace("_tracklist", "").rsplit(".", 1)[0],
                    title="Tracklist",
                    coverart_url=track.coverart_url,
                )
```

Delete the now-unused `print("    Found artwork, ...")` / `print("    No artwork found, ...")` pair, since the cached path no longer distinguishes those cases at this level.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -k reuses_cached -v`
Expected: PASS

- [ ] **Step 5: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: 267 passed (265 + 2); "All checks passed!"

Note: no existing test patches `setlist_maker.cli.fetch_artwork`, so moving the
call into `artwork_cache` breaks nothing. `embed_chapters_for_tracklist` is only
ever mocked out today (`tests/test_cli.py:273,283`) — the test added in Step 1 is
the first to exercise its body, so treat an unexpected failure here as a real
finding about the function rather than as fallout from the refactor.

- [ ] **Step 6: Update the docs**

In `CLAUDE.md`, in the `chapters.py` + `artwork.py` section, append to the `fetch_artwork()` bullet:

```markdown
- Chapter composites are produced through `artwork_cache.chapter_image()`, not by
  pairing `fetch_artwork()` + `create_chapter_image()` directly — that shared cache is
  what makes the web editor's preview byte-identical to what gets embedded.
```

In `README.md`, after line 103 (the sidecar/cover-art-URL note), add:

```markdown
Artwork previewed in the web editor is cached in `~/.cache/setlist-maker/artwork`
and reused by `chapters`, so what you approve on screen is exactly what gets
embedded — and a `--chapters` run right after editing needs no network.
```

- [ ] **Step 7: Commit**

```bash
git add setlist_maker/cli.py tests/test_cli.py CLAUDE.md README.md
git commit -m "feat(chapters): embed the same composite the editor previewed"
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| `artwork_cache.py`: `cache_dir`, `cache_key` | 1 |
| `chapter_image()`, structural invalidation, per-key lock, atomic write | 2 |
| Semaphore capping generation, hits unblocked | 2 |
| `GET /api/artwork?index=N`, index-based rationale, 404 table | 3 |
| Rejected tracks still served | 3 (no rejection check in `_send_artwork`) |
| Lazy `IntersectionObserver`, placeholder, click overlay, no-`innerHTML` | 4 |
| Re-render after save | 4 (step 5) |
| Chapters path reuse + episode cover | 5 |
| Error handling: no art / corrupt / unwritable / network | 2 (tests + impl) |
| Testing plan | 1–5 |

**Two deviations from the spec, both deliberate:**

1. The spec put the semaphore in the endpoint; the plan puts it in `artwork_cache`. That is what actually delivers the spec's stated property ("cache hits are served without taking the semaphore"), and the chapters path inherits the cap for free.
2. ~~The spec described a cache-busting query param for post-save refresh. The plan uses `Cache-Control: no-store` plus a re-render instead — same guarantee, no URL bookkeeping.~~ **Retracted during Task 4 browser verification: this was wrong.** `no-store` governs the HTTP cache, but Chrome still reuses an already-decoded image for an identical URL within the same document, so after edit-and-save the thumbnail kept showing the pre-edit composite until a full reload. The spec's cache-busting param was correct and is restored: the page keeps an `artVersion` counter, bumps it on every successful save, and appends `&v=<artVersion>` to the artwork URL. The server ignores the extra param (only `index` is read), so no endpoint change is needed.

**Client-side concurrency cap:** the spec called for 4 in flight. The plan relies on `IntersectionObserver` with a 200px `rootMargin`, which bounds in-flight requests to roughly a viewport's worth of rows, and the server-side semaphore is the real backstop. No separate client-side queue is built — YAGNI; add one only if a fast scroll proves it necessary.

**Type consistency:** `chapter_image(artist, title, coverart_url=None, size=CHAPTER_IMAGE_SIZE) -> bytes` is used identically in Tasks 2, 3 and 5. `cache_key(artist, title, coverart_url, size) -> str` and `cache_dir() -> Path` are used identically in Tasks 1 and 2.
