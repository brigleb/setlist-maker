"""Images the user uploaded in the web editor, kept beside the tracklist.

Two kinds, both plain files next to ``<set>_tracklist.md`` so they travel with
the set (iCloud included) rather than living in a cache that can be cleared:

- **Track artwork**, in ``<set>_artwork/<hash>.jpg``. A track points at one
  with ``coverart_url = "upload:<hash>.jpg"`` -- a reference, not a URL, so
  ``download_image`` (which refuses anything but http(s)) is never asked to
  fetch it; ``artwork_cache.source_artwork`` resolves it instead. Named by a
  hash of the stored bytes, so the name is also the artwork cache's key and a
  re-upload of the same picture is the same file.
- **The episode cover**, ``<set>_cover.jpg``. It is a property of the set, and
  the JSON sidecar is a bare list of tracks that must stay one (see CLAUDE.md,
  "The JSON sidecar's shape"), so the file's presence *is* the setting.

References come back from a hand-editable sidecar, so every read validates the
name against ``_REF`` before it goes anywhere near a path.
"""

import hashlib
import io
import logging
import os
import re
import tempfile
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

UPLOAD_PREFIX = "upload:"
_REF = re.compile(r"^upload:([0-9a-f]{24})\.jpg$")

# Refused before the body is read: a cover photo straight off a camera is a few
# MB, and nothing an editor needs is bigger than this.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Stored at up to this edge. The chapter composite is 600px; the headroom is for
# the episode cover, which podcast apps show larger.
MAX_STORED_EDGE = 1400


class UploadError(ValueError):
    """The upload is not an image this tool can read."""


def set_stem(tracklist_path: Path) -> str:
    """``2026-09-23-Keys-Lounge`` for ``2026-09-23-Keys-Lounge_tracklist.md``.

    The same rule ``web_editor.progress_path_for`` uses for the progress file.
    """
    name = tracklist_path.name
    if name.endswith("_tracklist.md"):
        return name[: -len("_tracklist.md")]
    return tracklist_path.stem


def artwork_dir_for(tracklist_path: Path) -> Path:
    """Where this set's uploaded track artwork lives (not created)."""
    return tracklist_path.with_name(f"{set_stem(tracklist_path)}_artwork")


def cover_path_for(tracklist_path: Path) -> Path:
    """Where this set's uploaded episode cover lives, if it has one."""
    return tracklist_path.with_name(f"{set_stem(tracklist_path)}_cover.jpg")


def is_upload_ref(value: str | None) -> bool:
    """True for a well-formed ``upload:<hash>.jpg`` reference."""
    return bool(value) and _REF.match(value) is not None


def upload_path(artwork_dir: Path, ref: str) -> Path | None:
    """The file a reference names, or None if the reference is malformed."""
    m = _REF.match(ref or "")
    return artwork_dir / f"{m.group(1)}.jpg" if m else None


def read_upload(artwork_dir: Path | None, ref: str) -> bytes | None:
    """The stored bytes for a reference, or None if malformed or missing."""
    if artwork_dir is None:
        return None
    path = upload_path(artwork_dir, ref)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def normalize_image(data: bytes, max_edge: int = MAX_STORED_EDGE) -> bytes:
    """Decode, centre-crop to square, cap the edge, and re-encode as JPEG.

    Square because every consumer is square and ``create_chapter_image``
    resizes rather than crops -- a portrait photo would be squashed. Re-encoding
    also means what is stored is known to decode, carries no metadata, and has
    one format for the ``.jpg`` name to be true about.
    """
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        # DecompressionBombError is not an OSError. HEIC (an iPhone's default)
        # lands here too: Pillow cannot read it without a plugin.
        raise UploadError(
            "not an image this tool can read (JPEG, PNG, WebP or GIF work; "
            "HEIC photos need exporting as JPEG first)"
        ) from e
    width, height = image.size
    edge = min(width, height)
    if width != height:
        left, top = (width - edge) // 2, (height - edge) // 2
        image = image.crop((left, top, left + edge, top + edge))
    if edge > max_edge:
        image = image.resize((max_edge, max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=90, optimize=True)
    return buf.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def store_upload(artwork_dir: Path, data: bytes) -> str:
    """Normalize and store an uploaded image; return its ``upload:`` reference."""
    stored = normalize_image(data)
    name = hashlib.sha256(stored).hexdigest()[:24]
    path = artwork_dir / f"{name}.jpg"
    if not path.exists():
        _write_atomic(path, stored)
    return f"{UPLOAD_PREFIX}{name}.jpg"


def set_episode_cover(tracklist_path: Path, ref: str | None) -> None:
    """Make an uploaded image the set's episode cover, or remove it (``None``)."""
    cover = cover_path_for(tracklist_path)
    if ref is None:
        cover.unlink(missing_ok=True)
        return
    data = read_upload(artwork_dir_for(tracklist_path), ref)
    if data is None:
        raise UploadError(f"uploaded image {ref!r} is missing")
    _write_atomic(cover, data)
