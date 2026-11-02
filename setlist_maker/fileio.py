"""Small file helpers shared by every module that persists user data.

A leaf module -- standard library only -- so anything can import it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/setlist-maker``, else ``~/.config/setlist-maker``.

    Tests rely on the variable: ``isolated_user_config`` points it at a temp
    dir so no test can rewrite the user's real settings or corrections.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "setlist-maker"


def _read_umask() -> int:
    # os.umask can only be read by setting it. Done once, at import, because
    # the window where it is 0 is process-wide and the web editor is threaded.
    mask = os.umask(0)
    os.umask(mask)
    return mask


_UMASK = _read_umask()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so a reader never sees a partial file.

    Written to a temp file in the same directory, flushed to disk, then
    ``os.replace``d over the target: a crash, a full disk or a kill midway
    leaves the previous file intact, and the temp file is removed on failure.
    The replaced file keeps its permissions (``mkstemp`` would otherwise make
    it owner-only), and a symlink keeps pointing where it did.
    """
    path = Path(path)
    if path.is_symlink():
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o666 & ~_UMASK
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """:func:`atomic_write_bytes` for text."""
    atomic_write_bytes(path, text.encode(encoding))
