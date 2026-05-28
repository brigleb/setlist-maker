"""Tests for setlist_maker.audio module."""

from setlist_maker import AUDIO_EXTENSIONS
from setlist_maker.audio import format_timestamp, get_audio_file


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
