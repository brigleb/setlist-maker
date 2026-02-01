"""Tests for setlist_maker.processor module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from setlist_maker.processor import (
    FFmpegError,
    ProcessingConfig,
    build_filter_chain,
    check_ffmpeg,
    create_concat_file,
    get_audio_duration,
    get_ffmpeg_version,
    process_audio,
)


class TestProcessingConfig:
    """Tests for ProcessingConfig dataclass."""

    def test_default_values(self):
        """Test that defaults are sensible."""
        config = ProcessingConfig()

        assert config.silence_threshold_db == -50.0
        assert config.compressor_threshold_db == -18.0
        assert config.compressor_ratio == 3.0
        assert config.target_loudness == -16.0
        assert config.true_peak == -1.5
        assert config.bitrate == "192k"
        assert config.remove_silence is True
        assert config.apply_compression is True
        assert config.apply_normalization is True

    def test_custom_values(self):
        """Test creating config with custom values."""
        config = ProcessingConfig(
            target_loudness=-14.0,
            bitrate="320k",
            apply_compression=False,
        )

        assert config.target_loudness == -14.0
        assert config.bitrate == "320k"
        assert config.apply_compression is False
        # Other defaults preserved
        assert config.remove_silence is True


class TestBuildFilterChain:
    """Tests for build_filter_chain function."""

    def test_all_filters_enabled(self):
        """Test filter chain with all stages enabled."""
        config = ProcessingConfig()
        chain = build_filter_chain(config)

        assert "silenceremove" in chain
        assert "acompressor" in chain
        assert "loudnorm" in chain
        # Filters should be comma-separated
        assert chain.count(",") == 2

    def test_silence_removal_only(self):
        """Test filter chain with only silence removal."""
        config = ProcessingConfig(
            remove_silence=True,
            apply_compression=False,
            apply_normalization=False,
        )
        chain = build_filter_chain(config)

        assert "silenceremove" in chain
        assert "acompressor" not in chain
        assert "loudnorm" not in chain
        assert "," not in chain

    def test_compression_only(self):
        """Test filter chain with only compression."""
        config = ProcessingConfig(
            remove_silence=False,
            apply_compression=True,
            apply_normalization=False,
        )
        chain = build_filter_chain(config)

        assert "silenceremove" not in chain
        assert "acompressor" in chain
        assert "loudnorm" not in chain

    def test_normalization_only(self):
        """Test filter chain with only normalization."""
        config = ProcessingConfig(
            remove_silence=False,
            apply_compression=False,
            apply_normalization=True,
        )
        chain = build_filter_chain(config)

        assert "silenceremove" not in chain
        assert "acompressor" not in chain
        assert "loudnorm" in chain

    def test_no_filters(self):
        """Test filter chain with all stages disabled."""
        config = ProcessingConfig(
            remove_silence=False,
            apply_compression=False,
            apply_normalization=False,
        )
        chain = build_filter_chain(config)

        assert chain == ""

    def test_filter_values_included(self):
        """Test that config values are included in filter string."""
        config = ProcessingConfig(
            silence_threshold_db=-45.0,
            compressor_threshold_db=-20.0,
            compressor_ratio=4.0,
            target_loudness=-14.0,
            true_peak=-2.0,
        )
        chain = build_filter_chain(config)

        assert "-45.0dB" in chain or "-45dB" in chain
        assert "-20.0dB" in chain or "-20dB" in chain
        assert "ratio=4" in chain
        assert "I=-14" in chain
        assert "TP=-2" in chain


class TestCreateConcatFile:
    """Tests for create_concat_file function."""

    def test_creates_file(self, temp_dir):
        """Test that concat file is created."""
        files = [Path("/audio/track1.mp3"), Path("/audio/track2.mp3")]
        concat_path = temp_dir / "filelist.txt"

        create_concat_file(files, concat_path)

        assert concat_path.exists()

    def test_file_format(self, temp_dir):
        """Test concat file format."""
        files = [Path("/audio/track1.mp3"), Path("/audio/track2.mp3")]
        concat_path = temp_dir / "filelist.txt"

        create_concat_file(files, concat_path)

        content = concat_path.read_text()
        lines = content.strip().split("\n")

        assert len(lines) == 2
        assert lines[0].startswith("file '")
        assert lines[0].endswith("track1.mp3'")
        assert lines[1].endswith("track2.mp3'")

    def test_escapes_single_quotes(self, temp_dir):
        """Test that single quotes in paths are escaped."""
        files = [Path("/audio/it's a track.mp3")]
        concat_path = temp_dir / "filelist.txt"

        create_concat_file(files, concat_path)

        content = concat_path.read_text()
        # Single quote should be escaped as '\''
        assert "'\\''" in content

    def test_absolute_paths(self, temp_dir):
        """Test that absolute paths are used."""
        # Create actual files
        audio_file = temp_dir / "track.mp3"
        audio_file.write_bytes(b"dummy")
        concat_path = temp_dir / "filelist.txt"

        create_concat_file([audio_file], concat_path)

        content = concat_path.read_text()
        assert str(temp_dir) in content


class TestCheckFFmpeg:
    """Tests for check_ffmpeg function."""

    @patch("shutil.which")
    def test_ffmpeg_found(self, mock_which):
        """Test when ffmpeg is available."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        assert check_ffmpeg() is True

    @patch("shutil.which")
    def test_ffmpeg_not_found(self, mock_which):
        """Test when ffmpeg is not available."""
        mock_which.return_value = None
        assert check_ffmpeg() is False


class TestGetFFmpegVersion:
    """Tests for get_ffmpeg_version function."""

    @patch("subprocess.run")
    def test_returns_version(self, mock_run):
        """Test extracting version string."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ffmpeg version 6.0 Copyright (c) 2000-2023\nmore info"
        )

        version = get_ffmpeg_version()

        assert version == "ffmpeg version 6.0 Copyright (c) 2000-2023"

    @patch("subprocess.run")
    def test_returns_none_on_error(self, mock_run):
        """Test returns None when ffmpeg fails."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        version = get_ffmpeg_version()

        assert version is None

    @patch("subprocess.run")
    def test_handles_timeout(self, mock_run):
        """Test handles subprocess timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired("ffmpeg", 10)

        version = get_ffmpeg_version()

        assert version is None

    @patch("subprocess.run")
    def test_handles_not_found(self, mock_run):
        """Test handles ffmpeg not found."""
        mock_run.side_effect = FileNotFoundError()

        version = get_ffmpeg_version()

        assert version is None


class TestGetAudioDuration:
    """Tests for get_audio_duration function."""

    @patch("subprocess.run")
    def test_returns_duration(self, mock_run):
        """Test extracting duration."""
        mock_run.return_value = MagicMock(returncode=0, stdout="123.456\n")

        duration = get_audio_duration(Path("/audio/track.mp3"))

        assert duration == 123.456

    @patch("subprocess.run")
    def test_returns_none_on_error(self, mock_run):
        """Test returns None on ffprobe error."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        duration = get_audio_duration(Path("/audio/track.mp3"))

        assert duration is None

    @patch("subprocess.run")
    def test_handles_timeout(self, mock_run):
        """Test handles subprocess timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired("ffprobe", 30)

        duration = get_audio_duration(Path("/audio/track.mp3"))

        assert duration is None

    @patch("subprocess.run")
    def test_handles_invalid_output(self, mock_run):
        """Test handles non-numeric output."""
        mock_run.return_value = MagicMock(returncode=0, stdout="N/A\n")

        duration = get_audio_duration(Path("/audio/track.mp3"))

        assert duration is None


class TestProcessAudio:
    """Tests for process_audio function."""

    def test_raises_on_empty_input(self):
        """Test raises ValueError for empty input list."""
        with pytest.raises(ValueError, match="No input files"):
            process_audio([], Path("/output.mp3"))

    @patch("setlist_maker.processor.check_ffmpeg")
    def test_raises_on_missing_ffmpeg(self, mock_check):
        """Test raises FFmpegError when ffmpeg not available."""
        mock_check.return_value = False

        with pytest.raises(FFmpegError, match="FFmpeg not found"):
            process_audio([Path("/audio/track.mp3")], Path("/output.mp3"))

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_single_file_no_concat(self, mock_run, mock_check, temp_dir):
        """Test that single file doesn't use concat demuxer."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        process_audio([input_file], output_file)

        # Check the command
        call_args = mock_run.call_args[0][0]
        assert "-f" not in call_args or "concat" not in str(call_args)
        assert "-i" in call_args

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_multiple_files_uses_concat(self, mock_run, mock_check, temp_dir):
        """Test that multiple files use concat demuxer."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input1 = temp_dir / "input1.mp3"
        input2 = temp_dir / "input2.mp3"
        input1.write_bytes(b"dummy")
        input2.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        process_audio([input1, input2], output_file)

        # Check the command includes concat
        call_args = mock_run.call_args[0][0]
        assert "-f" in call_args
        concat_idx = call_args.index("-f")
        assert call_args[concat_idx + 1] == "concat"

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_applies_filter_chain(self, mock_run, mock_check, temp_dir):
        """Test that filter chain is applied."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        process_audio([input_file], output_file)

        call_args = mock_run.call_args[0][0]
        assert "-af" in call_args

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_no_filter_chain_when_disabled(self, mock_run, mock_check, temp_dir):
        """Test that no filter applied when all disabled."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        config = ProcessingConfig(
            remove_silence=False,
            apply_compression=False,
            apply_normalization=False,
        )

        process_audio([input_file], output_file, config=config)

        call_args = mock_run.call_args[0][0]
        assert "-af" not in call_args

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_output_codec_settings(self, mock_run, mock_check, temp_dir):
        """Test that output uses correct codec and bitrate."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        config = ProcessingConfig(bitrate="320k")
        process_audio([input_file], output_file, config=config)

        call_args = mock_run.call_args[0][0]
        assert "-c:a" in call_args
        codec_idx = call_args.index("-c:a")
        assert call_args[codec_idx + 1] == "libmp3lame"
        assert "-b:a" in call_args
        bitrate_idx = call_args.index("-b:a")
        assert call_args[bitrate_idx + 1] == "320k"

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_creates_output_directory(self, mock_run, mock_check, temp_dir):
        """Test that output directory is created if needed."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "subdir" / "nested" / "output.mp3"

        process_audio([input_file], output_file)

        assert output_file.parent.exists()

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_raises_on_ffmpeg_failure(self, mock_run, mock_check, temp_dir):
        """Test raises FFmpegError when ffmpeg fails."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=1, stderr="Error: invalid input")

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        with pytest.raises(FFmpegError, match="FFmpeg processing failed"):
            process_audio([input_file], output_file)

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_returns_output_path(self, mock_run, mock_check, temp_dir):
        """Test that function returns the output path."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        result = process_audio([input_file], output_file)

        assert result == output_file
