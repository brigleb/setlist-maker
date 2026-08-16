"""Disk cache for generated chapter images.

The web editor previews the same composite that ``chapters`` embeds, so both
call ``chapter_image()`` here rather than pairing ``fetch_artwork()`` with
``create_chapter_image()`` themselves. Caching the result makes the preview
authoritative: what the user approved in the editor is what gets written into
the MP3, and a post-editing ``--chapters`` run costs no network.
"""

import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from setlist_maker.artwork import (
    CHAPTER_IMAGE_SIZE,
    ArtworkCandidate,
    artwork_candidates,
    create_chapter_image,
    fetch_artwork,
)

logger = logging.getLogger(__name__)

# Field separator for the hashed key. A NUL byte cannot appear in artist,
# title or URL text, so field boundaries can never be ambiguous.
_KEY_SEP = "\0"

# Cap simultaneous fetching. Each miss can make up to six network calls, so
# an editor scrolling 60 rows must not open 60 fetch storms. Cache hits never
# take the semaphore -- only the fetch inside source_artwork() is capped;
# compositing (create_chapter_image) is local PIL work and is deliberately
# left uncapped.
_MAX_CONCURRENT_GENERATION = 4
_generation_slots = threading.Semaphore(_MAX_CONCURRENT_GENERATION)

# One lock per cache key so two requests for the same track generate once.
# RLock, not Lock: chapter_image() calls source_artwork() for the *same* key
# while still holding this lock (both derive their cache key from the same
# (artist, title, coverart_url, size)), so the same thread must be able to
# re-enter without deadlocking itself.
_key_locks: dict[str, object] = {}
_key_locks_guard = threading.Lock()


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


def _lock_for(key: str):
    """Return the (created-on-demand) reentrant lock guarding one cache key."""
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.RLock())


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


def _read_cached_source(path: Path) -> bytes | None:
    """Return cached raw artwork bytes, or None if absent/unreadable/empty.

    Unlike composites, source art fetched from the network isn't necessarily
    JPEG (Shazam/iTunes/Deezer/MusicBrainz may hand back PNG or WebP), so no
    format validation is applied here -- only a zero-length file (the mark of
    an interrupted write) is treated as a miss.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if data else None


def _fallback_marker(key: str) -> Path:
    """Path of the empty marker recording that a lookup for this key found nothing."""
    return cache_dir() / f"{key}.fallback"


def _known_artless(key: str) -> bool:
    """True if an earlier lookup for this key already came back empty."""
    try:
        return _fallback_marker(key).exists()
    except OSError:
        # Path.exists() re-raises PermissionError rather than swallowing it,
        # unlike every other filesystem touch here. An unreadable cache has to
        # degrade to "don't know" -- try the fetch -- not crash the caller.
        return False


def _mark_artless(key: str) -> None:
    """Record that this key's lookup found nothing. Best-effort."""
    marker = _fallback_marker(key)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass  # an unwritable cache just means we look again next time


def source_artwork(
    artist: str,
    title: str,
    coverart_url: str | None = None,
    size: int = CHAPTER_IMAGE_SIZE,
) -> bytes | None:
    """Return the raw fetched cover art for a track, or None if none was found.

    Both answers are cached, because both are worth keeping: the bytes in
    ``<key>.src`` when a lookup succeeded, and an empty ``<key>.fallback``
    marker when it didn't. The marker is a negative-result cache -- it stops a
    track with no findable art from re-running the six-request waterfall on
    every run, and being a file it survives across processes, so the
    ``chapters`` run after an editing session still knows.

    Caching the source separately from the composite is what lets the episode
    cover reuse a track's fetched image under different overlay text without a
    second lookup.

    A ``None`` return means "no artwork for this track" and is the caller's to
    interpret; the episode cover uses it to skip artless tracks, the way it did
    before compositing moved behind this cache.
    """
    key = cache_key(artist, title, coverart_url, size)
    path = cache_dir() / f"{key}.src"

    # Cached bytes win over a marker: if a stale marker somehow coexists with
    # real source art, the art is the better answer.
    cached = _read_cached_source(path)
    if cached is not None:
        return cached
    if _known_artless(key):
        return None

    with _lock_for(key):
        # Another thread may have resolved this while we waited for the lock.
        cached = _read_cached_source(path)
        if cached is not None:
            return cached
        if _known_artless(key):
            return None

        with _generation_slots:
            artwork_bytes = fetch_artwork(
                artist=artist, title=title, coverart_url=coverart_url, size=size
            )

        if artwork_bytes is None:
            _mark_artless(key)
        else:
            _write_cached(path, artwork_bytes)
        return artwork_bytes


def _read_cached_candidates(path: Path) -> list[ArtworkCandidate] | None:
    """Return a cached candidate list, or None if absent, unreadable or stale-shaped."""
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    try:
        return [ArtworkCandidate(**item) for item in raw]
    except TypeError:
        # Written by a version with different fields: regenerate rather than
        # serve something the picker cannot render.
        return None


def artwork_options(
    artist: str,
    title: str,
    size: int = CHAPTER_IMAGE_SIZE,
) -> list[ArtworkCandidate]:
    """Return the alternate cover-art options for a track, cached on disk.

    Keyed *without* a ``coverart_url``: which alternates a search turns up
    depends on artist, title and size alone -- a saved URL is one particular
    answer, not an input to the question -- so the ``.cands`` entry sits beside
    the ``.src``/``.jpg`` files of the no-URL variant of the same key. Reopening
    the picker on a track is then free, which matters because the fan-out here
    is the expensive one: every source is asked, instead of stopping at the
    first that answers.

    An empty result is deliberately *not* cached, unlike ``source_artwork``'s
    ``.fallback`` marker. "No alternates" much more often means the network was
    down than that the track has none, and this is an interactive surface where
    retrying costs the user one click rather than a whole re-run.
    """
    key = cache_key(artist, title, None, size)
    path = cache_dir() / f"{key}.cands"

    cached = _read_cached_candidates(path)
    if cached is not None:
        return cached

    # Key lock first, then the semaphore -- the same order source_artwork()
    # takes, so the two paths cannot deadlock against each other.
    with _lock_for(key):
        cached = _read_cached_candidates(path)
        if cached is not None:
            return cached

        with _generation_slots:
            found = artwork_candidates(artist, title, size=size)

        if found:
            _write_cached(path, json.dumps([asdict(c) for c in found]).encode("utf-8"))
        return found


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

        artwork_bytes = source_artwork(artist, title, coverart_url, size)
        data = create_chapter_image(
            artwork_bytes=artwork_bytes, artist=artist, title=title, size=size
        )

        _write_cached(path, data)
        return data
