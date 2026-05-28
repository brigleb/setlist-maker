"""Tests for setlist_maker.playback (ffplay-based preview playback)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from setlist_maker.playback import (
    PREVIEW_SECONDS,
    PlaybackController,
    audio_output_available,
    playback_available,
    player_path,
)


def _fake_process(running=True):
    """Stand-in for subprocess.Popen: poll() is None while running."""
    proc = MagicMock()
    proc.poll.return_value = None if running else 0
    return proc


class TestAvailability:
    """Tests for capability detection."""

    @patch("setlist_maker.playback.shutil.which", return_value="/opt/homebrew/bin/ffplay")
    def test_player_path_returns_binary(self, _which):
        assert player_path() == "/opt/homebrew/bin/ffplay"

    @patch("setlist_maker.playback.shutil.which", return_value=None)
    def test_player_path_none_when_missing(self, _which):
        assert player_path() is None

    @patch("setlist_maker.playback.platform.system", return_value="Darwin")
    def test_macos_assumes_audio_device(self, _system):
        assert audio_output_available() is True

    @patch("setlist_maker.playback.platform.system", return_value="Linux")
    @patch("setlist_maker.playback.shutil.which", return_value="/usr/bin/pactl")
    @patch("setlist_maker.playback.subprocess.run")
    def test_linux_sink_present(self, mock_run, _which, _system):
        mock_run.return_value = MagicMock(returncode=0, stdout="0\talsa_output...\tRUNNING\n")
        assert audio_output_available() is True

    @patch("setlist_maker.playback.platform.system", return_value="Linux")
    @patch("setlist_maker.playback.shutil.which", return_value="/usr/bin/pactl")
    @patch("setlist_maker.playback.subprocess.run")
    def test_linux_no_sink_when_empty(self, mock_run, _which, _system):
        mock_run.return_value = MagicMock(returncode=0, stdout="   \n")
        assert audio_output_available() is False

    @patch("setlist_maker.playback.platform.system", return_value="Linux")
    @patch("setlist_maker.playback.subprocess.run")
    def test_linux_alsa_probe_enumerates_hardware_not_namehints(self, mock_run, _system):
        # When only ALSA is present, we must probe real cards (`aplay -l`, which
        # exits non-zero with no soundcards), NOT the namehint listing
        # (`aplay -L`, which lists `null`/`default` on any alsa-lib host and so
        # falsely reports audio on a headless box).
        def which(name):
            return "/usr/bin/aplay" if name == "aplay" else None

        with patch("setlist_maker.playback.shutil.which", side_effect=which):
            mock_run.return_value = MagicMock(returncode=1, stdout="")  # no soundcards found
            assert audio_output_available() is False
            assert mock_run.call_args[0][0] == ["aplay", "-l"]

    @patch("setlist_maker.playback.player_path", return_value="/usr/bin/ffplay")
    @patch("setlist_maker.playback.audio_output_available", return_value=True)
    def test_available_when_player_and_device(self, _dev, _path):
        assert playback_available() is True

    @patch("setlist_maker.playback.player_path", return_value=None)
    @patch("setlist_maker.playback.audio_output_available", return_value=True)
    def test_unavailable_when_no_player(self, _dev, _path):
        assert playback_available() is False


class TestPlaybackController:
    """Tests for the ffplay subprocess lifecycle."""

    @patch("setlist_maker.playback.subprocess.Popen")
    def test_play_spawns_ffplay_with_seek_and_duration(self, mock_popen):
        mock_popen.return_value = _fake_process()
        ctrl = PlaybackController()
        ctrl.play(Path("/music/set.mp3"), start_seconds=90)

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "ffplay"
        assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "90"
        assert "-t" in cmd and cmd[cmd.index("-t") + 1] == str(PREVIEW_SECONDS)
        assert "-nodisp" in cmd
        assert "-autoexit" in cmd
        assert cmd[-1] == "/music/set.mp3"

    @patch("setlist_maker.playback.subprocess.Popen")
    def test_is_playing_reflects_poll(self, mock_popen):
        proc = _fake_process(running=True)
        mock_popen.return_value = proc
        ctrl = PlaybackController()
        assert ctrl.is_playing() is False  # nothing started yet
        ctrl.play(Path("/music/set.mp3"), start_seconds=0)
        assert ctrl.is_playing() is True
        proc.poll.return_value = 0  # ffplay exited
        assert ctrl.is_playing() is False

    @patch("setlist_maker.playback.subprocess.Popen")
    def test_play_stops_previous_before_starting_new(self, mock_popen):
        first = _fake_process(running=True)
        second = _fake_process(running=True)
        mock_popen.side_effect = [first, second]
        ctrl = PlaybackController()

        ctrl.play(Path("/music/set.mp3"), start_seconds=0)
        ctrl.play(Path("/music/set.mp3"), start_seconds=120)

        first.terminate.assert_called_once()
        assert mock_popen.call_count == 2

    @patch("setlist_maker.playback.subprocess.Popen")
    def test_stop_terminates_running_process(self, mock_popen):
        proc = _fake_process(running=True)
        mock_popen.return_value = proc
        ctrl = PlaybackController()
        ctrl.play(Path("/music/set.mp3"), start_seconds=0)
        ctrl.stop()
        proc.terminate.assert_called_once()
        assert ctrl.is_playing() is False

    @patch("setlist_maker.playback.subprocess.Popen")
    def test_stop_reaps_process_after_terminate(self, mock_popen):
        # terminate() sends SIGTERM but does not reap; without a wait() the
        # child lingers as a zombie and Popen.__del__ emits a ResourceWarning.
        proc = _fake_process(running=True)
        mock_popen.return_value = proc
        ctrl = PlaybackController()
        ctrl.play(Path("/music/set.mp3"), start_seconds=0)
        ctrl.stop()
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()

    def test_stop_is_safe_when_nothing_playing(self):
        ctrl = PlaybackController()
        ctrl.stop()  # must not raise
        assert ctrl.is_playing() is False

    @patch("setlist_maker.playback.subprocess.Popen")
    def test_stop_does_not_terminate_finished_process(self, mock_popen):
        proc = _fake_process(running=False)  # already exited
        mock_popen.return_value = proc
        ctrl = PlaybackController()
        ctrl.play(Path("/music/set.mp3"), start_seconds=0)
        ctrl.stop()
        proc.terminate.assert_not_called()
