"""Shared pytest fixtures for setlist-maker tests."""

import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from setlist_maker.tracklist import CorrectionsDB, Track, Tracklist

# Hostnames a test may legitimately resolve: its own loopback server, nothing else.
_ALLOWED_HOSTS = frozenset({"", "127.0.0.1", "::1", "localhost", "localhost."})


class NetworkAccessBlocked(BaseException):
    """Raised when a test tries to resolve a real host.

    Deliberately a BaseException. Every artwork source helper wraps its request
    in ``except Exception`` and returns None, so an ordinary error here would be
    swallowed and the unpatched test would quietly pass with no artwork instead
    of reporting the leak -- exactly the silence this guard exists to end.
    """


def pytest_runtest_setup(item):
    """``@pytest.mark.requires_tool("ffprobe")``: skip without it -- except under CI (#66).

    The ffprobe round trips guard bugs that shipped (#17, #33) and the Node tests
    guard the timeline, so a CI runner missing either must fail, not go quietly green.
    """
    for mark in item.iter_markers("requires_tool"):
        program = mark.args[0]
        if shutil.which(program) is None:
            if os.environ.get("CI"):
                pytest.fail(f"{program} is not on PATH, and CI must not skip the tests needing it")
            pytest.skip(f"{program} is not installed")


@pytest.fixture(autouse=True)
def isolated_user_config(monkeypatch, tmp_path_factory):
    """Point ~/.config/setlist-maker/config.json somewhere disposable.

    Saving an episode remembers its artist there, and a test that saved one
    would otherwise rewrite the real user's default artist.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))


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


class SubprocessBlocked(BaseException):
    """Raised when a test tries to run a program that reaches beyond the machine.

    A BaseException for the same reason as ``NetworkAccessBlocked``:
    ``generate_summary()`` wraps its subprocess call in ``except Exception``
    and degrades to "no summary", so an ordinary error would let the test pass.
    """


# Programs a test may never launch: each one talks to a remote service, which
# the DNS guard cannot see because the lookup happens in another process.
_BLOCKED_PROGRAMS = frozenset({"claude"})


@pytest.fixture(autouse=True)
def block_remote_subprocesses(monkeypatch):
    """Fail any test that launches ``claude`` (a real LLM call, and ~4.5s).

    ``process_single_file()`` defaults to ``summary=True``, so a test that
    forgot ``summary=False`` used to spawn ``claude -p`` -- passing either way,
    and behaving differently on a machine where it is not installed. Popen is
    the choke point ``subprocess.run`` goes through; ffmpeg, ffprobe and node
    are local and pass untouched. A test that patches
    ``setlist_maker.summary.subprocess.run`` never reaches Popen at all.
    """

    class GuardedPopen(subprocess.Popen):
        def __init__(self, args, *a, **kw):
            argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
            program = os.path.basename(os.fsdecode(argv[0])).split()[0] if argv else ""
            if program in _BLOCKED_PROGRAMS:
                raise SubprocessBlocked(
                    f"test tried to run {program!r} -- pass summary=False, or patch "
                    f"'setlist_maker.summary.subprocess.run'"
                )
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)


@pytest.fixture(autouse=True)
def fast_server_shutdown(monkeypatch):
    """Make ``serve_forever()`` notice ``shutdown()`` in 50ms instead of 500ms.

    Every server a test starts -- its own, or production ``WatchSession``'s --
    blocks in ``serve_forever(poll_interval=0.5)``, and ``shutdown()`` waits
    out the poll. At ~80 server tests that was most of the suite's wall clock.
    Patched on the base class so it reaches every call site, including the
    thread ``WatchSession`` starts itself.
    """
    real = socketserver.BaseServer.serve_forever

    def serve_forever(self, poll_interval=0.05):
        return real(self, poll_interval)

    monkeypatch.setattr(socketserver.BaseServer, "serve_forever", serve_forever)


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


@pytest.fixture
def usage_error(monkeypatch, capsys):
    """Run the CLI on ``argv`` and expect argparse to refuse it.

    Returns stderr. Asserts what makes it a *usage* error rather than a failed
    run: exit status 2, and nothing on stdout.
    """
    from setlist_maker import cli

    def run(*argv: str) -> str:
        monkeypatch.setattr(sys, "argv", ["setlist-maker", *argv])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        out, err = capsys.readouterr()
        assert exc.value.code == 2, err
        assert out == ""
        return err

    return run
