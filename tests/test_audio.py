"""Tests for setlist_maker.audio module."""

import pytest

from setlist_maker import AUDIO_EXTENSIONS
from setlist_maker import audio as audio_module
from setlist_maker.audio import (
    TruncatedAudioError,
    format_timestamp,
    get_audio_file,
    load_audio,
    verify_decode_complete,
)


class _FakeSegment:
    """Stand-in for pydub.AudioSegment whose len() (ms) we control."""

    def __init__(self, milliseconds: int):
        self._ms = milliseconds

    def __len__(self) -> int:
        return self._ms


class TestFormatTimestamp:
    """Tests for format_timestamp function."""

    def test_zero_seconds(self):
        """Test formatting zero seconds."""
        assert format_timestamp(0) == "0:00"

    def test_seconds_only(self):
        """Test formatting less than a minute."""
        assert format_timestamp(45) == "0:45"

    def test_minutes_and_seconds(self):
        """Test formatting minutes and seconds."""
        assert format_timestamp(90) == "1:30"
        assert format_timestamp(125) == "2:05"

    def test_hours(self):
        """Test formatting with hours."""
        assert format_timestamp(3600) == "1:00:00"
        assert format_timestamp(3661) == "1:01:01"
        assert format_timestamp(7325) == "2:02:05"

    def test_large_values(self):
        """Test formatting large values."""
        # 10 hours, 30 minutes, 45 seconds
        assert format_timestamp(37845) == "10:30:45"


class TestGetAudioFile:
    """Tests for get_audio_file function."""

    def test_single_file(self, sample_audio_files):
        """Test validating a single audio file."""
        result = get_audio_file(str(sample_audio_files[0]))
        assert result is not None
        assert result.name == "track1.mp3"

    def test_directory_rejected(self, temp_dir, capsys):
        """A directory is not a file and is rejected."""
        result = get_audio_file(str(temp_dir))
        assert result is None
        assert "Not a file" in capsys.readouterr().out

    def test_non_audio_rejected(self, temp_dir, capsys):
        """A non-audio file is rejected with a clear message."""
        txt_file = temp_dir / "readme.txt"
        txt_file.write_text("not audio")

        result = get_audio_file(str(txt_file))

        assert result is None
        assert "Not a supported audio file" in capsys.readouterr().out

    def test_nonexistent_path(self, capsys):
        """Test handling of a nonexistent path."""
        result = get_audio_file("/nonexistent/path.mp3")

        assert result is None
        assert "Path not found" in capsys.readouterr().out

    def test_supported_extensions(self):
        """Test that AUDIO_EXTENSIONS includes common formats."""
        assert ".mp3" in AUDIO_EXTENSIONS
        assert ".wav" in AUDIO_EXTENSIONS
        assert ".flac" in AUDIO_EXTENSIONS
        assert ".m4a" in AUDIO_EXTENSIONS
        assert ".ogg" in AUDIO_EXTENSIONS


class TestVerifyDecodeComplete:
    """Tests for verify_decode_complete (the truncation-guard decision logic)."""

    def test_matching_durations_pass(self):
        """A decode that matches the reported duration does not raise."""
        verify_decode_complete(15148.0, 15148.0)

    def test_reported_none_is_skipped(self):
        """When ffprobe gives no duration, the check is skipped (no raise)."""
        verify_decode_complete(4680.0, None)

    def test_reported_zero_is_skipped(self):
        """A non-positive reported duration can't be compared; no raise."""
        verify_decode_complete(4680.0, 0.0)

    def test_small_shortfall_within_tolerance_passes(self):
        """A sub-tolerance gap (encoder padding / estimate noise) does not raise."""
        # 1s short on a ~2h file is well within tolerance.
        verify_decode_complete(7199.0, 7200.0)

    def test_decoded_longer_than_reported_passes(self):
        """A decode longer than the header estimate is never a truncation."""
        verify_decode_complete(7205.0, 7200.0)

    def test_gross_truncation_raises(self):
        """The real bug: 78 min decoded of a file that reports 4.2 h must raise."""
        with pytest.raises(TruncatedAudioError) as excinfo:
            verify_decode_complete(4680.0, 15148.0)
        # Message names both durations so the cause is obvious.
        message = str(excinfo.value)
        assert "1:18:00" in message  # decoded
        assert "4:12:28" in message  # reported


class TestLoadAudioGuard:
    """load_audio wires the decode-completeness guard around pydub."""

    def test_raises_when_decode_far_short_of_reported(self, tmp_path, monkeypatch):
        """A partially-materialized file (short decode, full reported size) aborts."""
        fake = tmp_path / "set.mp3"
        fake.write_bytes(b"x")
        monkeypatch.setattr(
            audio_module.AudioSegment, "from_file", lambda path: _FakeSegment(4680 * 1000)
        )
        monkeypatch.setattr(audio_module, "probe_duration_seconds", lambda path: 15148.0)

        with pytest.raises(TruncatedAudioError):
            load_audio(fake)

    def test_ok_when_decode_matches_reported(self, tmp_path, monkeypatch):
        """A fully-decoded file passes the guard and is returned."""
        fake = tmp_path / "set.mp3"
        fake.write_bytes(b"x")
        monkeypatch.setattr(
            audio_module.AudioSegment, "from_file", lambda path: _FakeSegment(15148 * 1000)
        )
        monkeypatch.setattr(audio_module, "probe_duration_seconds", lambda path: 15148.0)

        result = load_audio(fake)

        assert len(result) == 15148 * 1000

    def test_allow_partial_bypasses_guard(self, tmp_path, monkeypatch):
        """--allow-partial skips the check entirely (no ffprobe, no raise)."""
        fake = tmp_path / "set.mp3"
        fake.write_bytes(b"x")
        monkeypatch.setattr(
            audio_module.AudioSegment, "from_file", lambda path: _FakeSegment(4680 * 1000)
        )

        def _boom(path):
            raise AssertionError("probe_duration_seconds must not be called")

        monkeypatch.setattr(audio_module, "probe_duration_seconds", _boom)

        result = load_audio(fake, allow_partial=True)

        assert len(result) == 4680 * 1000
