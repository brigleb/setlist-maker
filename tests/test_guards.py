"""The suite's own guards: each must fire, or the suite is hermetic only by luck."""

import socket
import subprocess
import sys

import pytest

from setlist_maker.summary import generate_summary
from tests.conftest import NetworkAccessBlocked, SubprocessBlocked


def test_resolving_a_real_host_is_blocked():
    with pytest.raises(NetworkAccessBlocked):
        socket.getaddrinfo("example.com", 443)


@pytest.mark.parametrize(
    "argv", [["claude", "-p", "hi"], ["/usr/local/bin/claude", "-p"], "claude -p hi"]
)
def test_launching_claude_is_blocked(argv):
    with pytest.raises(SubprocessBlocked):
        subprocess.run(argv, shell=isinstance(argv, str))


def test_generate_summary_cannot_reach_the_real_cli(monkeypatch):
    """The guard is a BaseException, so summary.py's `except Exception` can't eat it."""
    monkeypatch.setattr("setlist_maker.summary.shutil.which", lambda name: "/usr/bin/claude")
    with pytest.raises(SubprocessBlocked):
        generate_summary(["Artist - Title"])


def test_local_programs_still_run():
    out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert out.stdout.strip() == "ok"
