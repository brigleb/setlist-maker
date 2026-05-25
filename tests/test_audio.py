"""Tests for setlist_maker.audio module."""

from setlist_maker import AUDIO_EXTENSIONS
from setlist_maker.audio import format_timestamp, get_audio_files


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


class TestGetAudioFiles:
    """Tests for get_audio_files function."""

    def test_single_file(self, sample_audio_files, temp_dir):
        """Test getting a single audio file."""
        files = get_audio_files([str(sample_audio_files[0])])
        assert len(files) == 1
        assert files[0].name == "track1.mp3"

    def test_multiple_files(self, sample_audio_files):
        """Test getting multiple audio files."""
        paths = [str(f) for f in sample_audio_files]
        files = get_audio_files(paths)
        assert len(files) == 3

    def test_directory(self, sample_audio_files, temp_dir):
        """Test getting files from a directory."""
        files = get_audio_files([str(temp_dir)])
        # Should find all 3 audio files, not the txt file
        assert len(files) == 3

    def test_filters_non_audio(self, temp_dir, capsys):
        """Test that non-audio files are filtered out."""
        txt_file = temp_dir / "readme.txt"
        txt_file.write_text("not audio")

        files = get_audio_files([str(txt_file)])

        assert len(files) == 0
        captured = capsys.readouterr()
        assert "Skipping non-audio file" in captured.out

    def test_nonexistent_path(self, capsys):
        """Test handling of nonexistent paths."""
        files = get_audio_files(["/nonexistent/path.mp3"])

        assert len(files) == 0
        captured = capsys.readouterr()
        assert "Path not found" in captured.out

    def test_mixed_valid_invalid(self, sample_audio_files, temp_dir, capsys):
        """Test mix of valid and invalid paths."""
        paths = [
            str(sample_audio_files[0]),
            "/nonexistent.mp3",
            str(temp_dir / "readme.txt"),
        ]
        files = get_audio_files(paths)

        assert len(files) == 1
        assert files[0].name == "track1.mp3"

    def test_supported_extensions(self):
        """Test that AUDIO_EXTENSIONS includes common formats."""
        assert ".mp3" in AUDIO_EXTENSIONS
        assert ".wav" in AUDIO_EXTENSIONS
        assert ".flac" in AUDIO_EXTENSIONS
        assert ".m4a" in AUDIO_EXTENSIONS
        assert ".ogg" in AUDIO_EXTENSIONS
