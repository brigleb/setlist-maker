"""Disk cache for generated chapter images.

The web editor previews the same composite that ``chapters`` embeds, so both
call ``chapter_image()`` here rather than pairing ``fetch_artwork()`` with
``create_chapter_image()`` themselves. Caching the result makes the preview
authoritative: what the user approved in the editor is what gets written into
the MP3, and a post-editing ``--chapters`` run costs no network.
"""

import hashlib
import logging
import os
import tempfile
import threading
from pathlib import Path

from setlist_maker.artwork import CHAPTER_IMAGE_SIZE, create_chapter_image, fetch_artwork

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
_key_locks: dict[str, threading.RLock] = {}
_key_locks_guard = threading.Lock()

# Keys whose composite was drawn on the gradient because no source artwork was
# found. Mirrored to a marker file so a later process (the `chapters` run after
# an editing session) still knows, and does not pick a gradient as the episode
# cover. The in-process set also covers the case where the cache is unwritable.
_fallback_seen: set[str] = set()


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


def _lock_for(key: str) -> threading.RLock:
    """Return the (created-on-demand) lock guarding one cache key."""
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


def source_artwork(
    artist: str,
    title: str,
    coverart_url: str | None = None,
    size: int = CHAPTER_IMAGE_SIZE,
) -> bytes | None:
    """Return the raw fetched cover art for a track, or None if none was found.

    Cached separately from the composite so a second composite of the same
    track (the episode cover, which carries different text) reuses the
    fetched image instead of running the waterfall again.
    """
    key = cache_key(artist, title, coverart_url, size)
    path = cache_dir() / f"{key}.src"

    cached = _read_cached_source(path)
    if cached is not None:
        return cached

    with _lock_for(key):
        # Another thread may have fetched this while we waited for the lock.
        cached = _read_cached_source(path)
        if cached is not None:
            return cached

        with _generation_slots:
            artwork_bytes = fetch_artwork(
                artist=artist, title=title, coverart_url=coverart_url, size=size
            )

        # Record fallback-ness BEFORE writing the source file, for the same
        # fail-safe reason chapter_image() records before writing its
        # composite: a crash after the write must not leave a hit that
        # silently forgets whether real art was found.
        _record_fallback(key, artwork_bytes is None)
        if artwork_bytes is not None:
            _write_cached(path, artwork_bytes)
        return artwork_bytes


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

        # source_artwork() records fallback-ness itself (before it returns),
        # so that fail-safe ordering is already satisfied by the time we get
        # here -- this call must happen before the composite is written below.
        artwork_bytes = source_artwork(artist, title, coverart_url, size)
        data = create_chapter_image(
            artwork_bytes=artwork_bytes, artist=artist, title=title, size=size
        )

        _write_cached(path, data)
        return data


def _fallback_marker(key: str) -> Path:
    return cache_dir() / f"{key}.fallback"


def _record_fallback(key: str, is_fallback: bool) -> None:
    """Persist whether this composite was drawn without real source artwork."""
    marker = _fallback_marker(key)
    if is_fallback:
        # Don't mark a key as fallback if a composite is already cached for
        # it. A cached .jpg was either built from real art (a marker here
        # would be wrong, and -- because chapter_image() hits the .jpg cache
        # and returns before ever reaching this line again -- permanently
        # wrong, hiding that track from the episode cover forever) or it was
        # itself a fallback, in which case its marker already exists and
        # skipping is harmless. This guards the case where a .jpg is cached
        # but its .src is missing (an older build, a pruned .src, or an
        # unwritable cache) and a later re-fetch attempt fails.
        #
        # Path.exists() only swallows the OSError subtypes matching ENOENT /
        # ENOTDIR / EBADF / ELOOP -- it re-raises PermissionError (EACCES),
        # unlike every other filesystem touch in this module, so this check
        # needs its own guard rather than relying on exists()'s own handling.
        try:
            composite_exists = (cache_dir() / f"{key}.jpg").exists()
        except OSError:
            # Can't tell whether a composite is cached -- the cache is
            # unusable in this state anyway (the marker write below would
            # fail too). Record the fallback in-process so used_fallback()
            # stays correct for the rest of this run, and skip the disk
            # write entirely. This add() must happen here, not be left to
            # fall through into some outer handler, or an unreadable cache
            # dir would silently make used_fallback() wrong instead of just
            # slow.
            _fallback_seen.add(key)
            return

        if composite_exists:
            return

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
    if key in _fallback_seen:
        return True
    try:
        return _fallback_marker(key).exists()
    except OSError:
        # Same PermissionError hazard _record_fallback() guards against:
        # Path.exists() re-raises EACCES rather than swallowing it, so an
        # unreadable cache dir would crash the caller (`chapters`) instead of
        # degrading. Nothing in _fallback_seen and no readable marker means we
        # cannot prove this key was a fallback, so report False -- the episode
        # cover then re-checks for real source bytes before using the track.
        return False
