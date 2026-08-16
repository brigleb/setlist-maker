"""Shared pytest fixtures for setlist-maker tests."""

import socket
import tempfile
import time
from pathlib import Path

import pytest

from setlist_maker.editor import CorrectionsDB, Track, Tracklist

# Hostnames a test may legitimately resolve: its own loopback server, nothing else.
_ALLOWED_HOSTS = frozenset({"", "127.0.0.1", "::1", "localhost", "localhost."})


class NetworkAccessBlocked(BaseException):
    """Raised when a test tries to resolve a real host.

    Deliberately a BaseException. Every artwork source helper wraps its request
    in ``except Exception`` and returns None, so an ordinary error here would be
    swallowed and the unpatched test would quietly pass with no artwork instead
    of reporting the leak -- exactly the silence this guard exists to end.
    """


@pytest.fixture(autouse=True)
def block_outbound_network(monkeypatch):
    """Fail any test that tries to reach a real host.

    Nothing structurally stopped a test from hitting iTunes, Deezer or the
    Cover Art Archive: one that forgot to patch its seam simply passed --
    slowly, at a rate limiter's mercy, and only until the machine had no
    network. That risk grew with the artwork picker, which asks every source
    instead of stopping at the first that answers.

    Name resolution is the choke point every outbound connection goes through,
    so refusing to resolve anything but loopback catches the mistake where it
    is made and names the fix. The web editor's own tests talk to a loopback
    server on an ephemeral port and are unaffected.
    """
    real_getaddrinfo = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if host is not None and str(host).lower() not in _ALLOWED_HOSTS:
            raise NetworkAccessBlocked(
                f"test tried to resolve {host!r} -- patch the network seam instead: "
                f"'setlist_maker.artwork.urllib.request.urlopen' for a source helper, "
                f"or fetch_artwork/artwork_candidates in the module that imported it"
            )
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def wait_until():
    """Textual pilot helper: keep pausing until ``predicate()`` holds.

    ``pilot.press()`` / ``pilot.pause()`` only wait for messages that were
    already queued when they were called, plus a heuristic idle check that
    treats "little CPU used in the last 20 ms" as "settled". A chain like Enter
    -> Input.Submitted -> focus() -> deferred set_focus hops several queues, and
    on a loaded machine a CPU-starved process looks idle by that heuristic
    while its queues are still full -- so the next key could be sent before
    focus had moved (#35). Waiting on the condition a step actually depends on
    removes the timing assumption rather than padding it with sleeps. Fails
    with an AssertionError naming ``what`` on timeout, so it reads like the
    plain assert it replaces.

    Usage inside a pilot routine::

        await wait_until(pilot, lambda: app.focused is title_input, what="focus on title")
    """

    async def _wait_until(pilot, predicate, *, timeout: float = 5.0, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out after {timeout}s waiting for {what}")
            await pilot.pause()

    return _wait_until


@pytest.fixture
def sample_track():
    """A basic Track instance."""
    return Track(
        timestamp=90,
        artist="Daft Punk",
        title="Around the World",
    )


@pytest.fixture
def sample_tracklist():
    """A Tracklist with several tracks."""
    return Tracklist(
        source_file="test_mix.mp3",
        generated_on="2026-01-31 20:00",
        tracks=[
            Track(timestamp=0, artist="Daft Punk", title="Around the World"),
            Track(timestamp=180, artist="The Chemical Brothers", title="Block Rockin' Beats"),
            Track(timestamp=360, artist="", title=""),  # Unidentified
            Track(timestamp=540, artist="Fatboy Slim", title="Praise You"),
        ],
    )


@pytest.fixture
def temp_dir():
    """Provide a temporary directory that's cleaned up after the test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_corrections_db(temp_dir):
    """A CorrectionsDB using a temporary file."""
    db_path = temp_dir / "corrections.json"
    return CorrectionsDB(db_path=db_path)


@pytest.fixture
def sample_markdown():
    """Sample markdown tracklist content."""
    return """# Tracklist: test_mix.mp3

*Generated on 2026-01-31 20:00*

1. **Daft Punk** - Around the World (0:00)
2. **The Chemical Brothers** - Block Rockin' Beats (3:00)
3. *Unidentified* (6:00)
4. **Fatboy Slim** - Praise You (9:00)
"""


@pytest.fixture
def sample_audio_files(temp_dir):
    """Create dummy audio files for testing file discovery."""
    files = []
    for name in ["track1.mp3", "track2.wav", "track3.flac"]:
        path = temp_dir / name
        path.write_bytes(b"dummy audio content")
        files.append(path)

    # Also create a non-audio file
    (temp_dir / "readme.txt").write_text("not an audio file")

    return files
