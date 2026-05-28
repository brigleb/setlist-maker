"""Tests for setlist_maker.summary module."""

import subprocess
from unittest.mock import MagicMock, patch

from setlist_maker.summary import generate_summary


def _completed(returncode=0, stdout="", stderr=""):
    """Build a stand-in for subprocess.run's CompletedProcess result."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestGenerateSummary:
    """Tests for generate_summary()."""

    def test_returns_none_for_empty_tracklist(self):
        """No tracks means no work; the CLI is never invoked."""
        with patch("setlist_maker.summary.subprocess.run") as mock_run:
            assert generate_summary([]) is None
            mock_run.assert_not_called()

    @patch("setlist_maker.summary.shutil.which", return_value=None)
    def test_returns_none_when_cli_missing(self, _mock_which):
        """A missing 'claude' CLI is reported and skipped, not fatal."""
        with patch("setlist_maker.summary.subprocess.run") as mock_run:
            assert generate_summary(["Artist - Title"]) is None
            mock_run.assert_not_called()

    @patch("setlist_maker.summary.shutil.which", return_value="/usr/bin/claude")
    @patch("setlist_maker.summary.subprocess.run")
    def test_returns_stripped_summary_on_success(self, mock_run, _mock_which):
        """A successful run returns the trimmed stdout paragraph."""
        mock_run.return_value = _completed(stdout="  A driving techno set.\n\n")
        assert generate_summary(["Artist - Title"]) == "A driving techno set."

    @patch("setlist_maker.summary.shutil.which", return_value="/usr/bin/claude")
    @patch("setlist_maker.summary.subprocess.run")
    def test_invocation_is_isolated_and_includes_tracks(self, mock_run, _mock_which):
        """The call uses --strict-mcp-config, a throwaway cwd, and the track list."""
        mock_run.return_value = _completed(stdout="Summary.")
        generate_summary(["Daft Punk - Around the World", "Fatboy Slim - Praise You"])

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--strict-mcp-config" in cmd
        # Prompt is the final positional CLI arg and carries the tracklist.
        prompt = cmd[-1]
        assert "Daft Punk - Around the World" in prompt
        assert "Fatboy Slim - Praise You" in prompt
        # Runs from an isolated working directory so project config can't leak in.
        assert kwargs["cwd"]
        assert kwargs["timeout"] > 0

    @patch("setlist_maker.summary.shutil.which", return_value="/usr/bin/claude")
    @patch("setlist_maker.summary.subprocess.run")
    def test_returns_none_on_nonzero_exit(self, mock_run, _mock_which):
        """A non-zero exit code is treated as failure."""
        mock_run.return_value = _completed(returncode=1, stderr="boom")
        assert generate_summary(["Artist - Title"]) is None

    @patch("setlist_maker.summary.shutil.which", return_value="/usr/bin/claude")
    @patch("setlist_maker.summary.subprocess.run")
    def test_returns_none_on_empty_output(self, mock_run, _mock_which):
        """Whitespace-only output is treated as no summary."""
        mock_run.return_value = _completed(stdout="   \n  ")
        assert generate_summary(["Artist - Title"]) is None

    @patch("setlist_maker.summary.shutil.which", return_value="/usr/bin/claude")
    @patch("setlist_maker.summary.subprocess.run")
    def test_returns_none_on_timeout(self, mock_run, _mock_which):
        """A timeout is caught and skipped rather than propagated."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        assert generate_summary(["Artist - Title"]) is None

    @patch("setlist_maker.summary.shutil.which", return_value="/usr/bin/claude")
    @patch("setlist_maker.summary.subprocess.run")
    def test_returns_none_on_unexpected_error(self, mock_run, _mock_which):
        """Any other subprocess error is caught and skipped."""
        mock_run.side_effect = OSError("no such file")
        assert generate_summary(["Artist - Title"]) is None
