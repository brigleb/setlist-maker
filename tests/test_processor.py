"""Tests for setlist_maker.processor module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from setlist_maker.processor import (
    ContentBoundaries,
    FFmpegError,
    ProcessingConfig,
    _get_sample_rate,
    build_filter_chain,
    check_ffmpeg,
    create_concat_file,
    detect_content_boundaries,
    get_audio_duration,
    get_ffmpeg_version,
    process_audio,
)


class TestProcessingConfig:
    """Tests for ProcessingConfig dataclass."""

    def test_default_values(self):
        """Test that defaults are sensible."""
        config = ProcessingConfig()

        assert config.trim_threshold_db == -50.0
        assert config.trim_chunk_duration == 5.0
        assert config.trim_consecutive_chunks == 3
        assert config.trim_padding_seconds == 2.0
        assert config.fade_in_duration == 3.0
        assert config.fade_out_duration == 3.0
        assert config.compressor_threshold_db == -18.0
        assert config.compressor_ratio == 3.0
        assert config.target_loudness == -16.0
        assert config.true_peak == -1.5
        assert config.bitrate == "192k"
        assert config.trim_silence is True
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
        assert config.trim_silence is True


class TestBuildFilterChain:
    """Tests for build_filter_chain function."""

    def test_all_filters_no_boundaries(self):
        """Test filter chain with all stages enabled but no boundaries."""
        config = ProcessingConfig()
        chain = build_filter_chain(config)

        # No silenceremove anymore
        assert "silenceremove" not in chain
        # No fades without boundaries
        assert "afade" not in chain
        assert "acompressor" in chain
        assert "loudnorm" in chain
        assert chain.count(",") == 1

    def test_fades_with_boundaries(self):
        """Test that fades are added when boundaries are provided."""
        config = ProcessingConfig()
        boundaries = ContentBoundaries(content_start=10.0, content_end=310.0, total_duration=350.0)
        chain = build_filter_chain(config, boundaries)

        assert "afade=t=in:d=3" in chain
        assert "afade=t=out:st=297.0:d=3" in chain
        assert "acompressor" in chain
        assert "loudnorm" in chain

    def test_fades_clamped_to_content_duration(self):
        """Test that fades are reduced if content is short."""
        config = ProcessingConfig(fade_in_duration=10.0, fade_out_duration=10.0)
        boundaries = ContentBoundaries(content_start=0.0, content_end=8.0, total_duration=8.0)
        chain = build_filter_chain(config, boundaries)

        # Each fade should be at most half the content duration (4s)
        assert "afade=t=in:d=4.0" in chain
        assert "afade=t=out:st=4.0:d=4.0" in chain

    def test_no_fades_when_trim_disabled(self):
        """Test no fades when trim_silence is False, even with boundaries."""
        config = ProcessingConfig(trim_silence=False)
        boundaries = ContentBoundaries(content_start=10.0, content_end=310.0, total_duration=350.0)
        chain = build_filter_chain(config, boundaries)

        assert "afade" not in chain

    def test_compression_only(self):
        """Test filter chain with only compression."""
        config = ProcessingConfig(
            trim_silence=False,
            apply_compression=True,
            apply_normalization=False,
        )
        chain = build_filter_chain(config)

        assert "acompressor" in chain
        assert "loudnorm" not in chain

    def test_normalization_only(self):
        """Test filter chain with only normalization."""
        config = ProcessingConfig(
            trim_silence=False,
            apply_compression=False,
            apply_normalization=True,
        )
        chain = build_filter_chain(config)

        assert "acompressor" not in chain
        assert "loudnorm" in chain

    def test_no_filters(self):
        """Test filter chain with all stages disabled."""
        config = ProcessingConfig(
            trim_silence=False,
            apply_compression=False,
            apply_normalization=False,
        )
        chain = build_filter_chain(config)

        assert chain == ""

    def test_filter_values_included(self):
        """Test that config values are included in filter string."""
        config = ProcessingConfig(
            trim_silence=False,
            compressor_threshold_db=-20.0,
            compressor_ratio=4.0,
            target_loudness=-14.0,
            true_peak=-2.0,
        )
        chain = build_filter_chain(config)

        assert "-20.0dB" in chain or "-20dB" in chain
        assert "ratio=4" in chain
        assert "I=-14" in chain
        assert "TP=-2" in chain


class TestGetSampleRate:
    """Tests for _get_sample_rate function."""

    @patch("subprocess.run")
    def test_returns_sample_rate(self, mock_run):
        """Test extracting sample rate."""
        mock_run.return_value = MagicMock(returncode=0, stdout="48000\n")

        rate = _get_sample_rate(Path("/audio/track.wav"))

        assert rate == 48000

    @patch("subprocess.run")
    def test_returns_default_on_error(self, mock_run):
        """Test returns 44100 on ffprobe error."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        rate = _get_sample_rate(Path("/audio/track.wav"))

        assert rate == 44100

    @patch("subprocess.run")
    def test_handles_timeout(self, mock_run):
        """Test handles subprocess timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired("ffprobe", 30)

        rate = _get_sample_rate(Path("/audio/track.wav"))

        assert rate == 44100


class TestDetectContentBoundaries:
    """Tests for detect_content_boundaries function."""

    def _make_rms_stderr(self, rms_values: list[float]) -> str:
        """Build fake ffmpeg stderr with RMS_level values."""
        lines = []
        for val in rms_values:
            if val == float("-inf"):
                lines.append("lavfi.astats.Overall.RMS_level=-inf")
            else:
                lines.append(f"lavfi.astats.Overall.RMS_level={val}")
        return "\n".join(lines)

    @patch("setlist_maker.processor._get_sample_rate", return_value=44100)
    @patch("setlist_maker.processor.get_audio_duration")
    @patch("subprocess.run")
    def test_detects_music_start_and_end(self, mock_run, mock_duration, mock_rate):
        """Test that content boundaries are correctly detected."""
        mock_duration.return_value = 100.0  # 20 chunks of 5s

        # 5 noise chunks, 10 music chunks, 5 noise chunks
        rms = [-70.0] * 5 + [-30.0] * 10 + [-70.0] * 5
        mock_run.return_value = MagicMock(returncode=0, stderr=self._make_rms_stderr(rms))

        result = detect_content_boundaries(
            Path("/audio/test.wav"),
            threshold_db=-50.0,
            chunk_duration=5.0,
            required_consecutive=3,
            padding_seconds=2.0,
        )

        # Music starts at chunk 5 (25s) minus 2s padding = 23s
        assert result.content_start == 23.0
        # Music ends at chunk 14 (75s) plus 2s padding = 77s
        assert result.content_end == 77.0
        assert result.total_duration == 100.0

    @patch("setlist_maker.processor._get_sample_rate", return_value=44100)
    @patch("setlist_maker.processor.get_audio_duration")
    @patch("subprocess.run")
    def test_transient_immunity(self, mock_run, mock_duration, mock_rate):
        """Test that isolated transient spikes don't trigger detection."""
        mock_duration.return_value = 50.0  # 10 chunks

        # All noise except one transient spike at chunk 3
        rms = [-70.0, -70.0, -70.0, -30.0, -70.0, -30.0, -30.0, -30.0, -70.0, -70.0]
        mock_run.return_value = MagicMock(returncode=0, stderr=self._make_rms_stderr(rms))

        result = detect_content_boundaries(
            Path("/audio/test.wav"),
            threshold_db=-50.0,
            chunk_duration=5.0,
            required_consecutive=3,
            padding_seconds=2.0,
        )

        # Should detect the run of 3 at chunks 5-7, not the single at chunk 3
        assert result.content_start == 23.0  # chunk 5 (25s) - 2s padding
        assert result.content_end == 42.0  # chunk 7+1 (40s) + 2s padding

    @patch("setlist_maker.processor._get_sample_rate", return_value=44100)
    @patch("setlist_maker.processor.get_audio_duration")
    @patch("subprocess.run")
    def test_all_music_returns_full_duration(self, mock_run, mock_duration, mock_rate):
        """Test that all-music input returns full duration."""
        mock_duration.return_value = 50.0

        rms = [-30.0] * 10
        mock_run.return_value = MagicMock(returncode=0, stderr=self._make_rms_stderr(rms))

        result = detect_content_boundaries(
            Path("/audio/test.wav"),
            threshold_db=-50.0,
            chunk_duration=5.0,
            required_consecutive=3,
            padding_seconds=2.0,
        )

        # Content starts at 0 (clamped), ends at full duration (clamped)
        assert result.content_start == 0.0
        assert result.content_end == 50.0

    @patch("setlist_maker.processor._get_sample_rate", return_value=44100)
    @patch("setlist_maker.processor.get_audio_duration")
    @patch("subprocess.run")
    def test_all_noise_returns_full_duration(self, mock_run, mock_duration, mock_rate):
        """Test that all-noise input falls back to full duration."""
        mock_duration.return_value = 50.0

        rms = [-70.0] * 10
        mock_run.return_value = MagicMock(returncode=0, stderr=self._make_rms_stderr(rms))

        result = detect_content_boundaries(
            Path("/audio/test.wav"),
            threshold_db=-50.0,
            chunk_duration=5.0,
            required_consecutive=3,
            padding_seconds=2.0,
        )

        # Safety fallback: don't trim anything
        assert result.content_start == 0.0
        assert result.content_end == 50.0

    @patch("setlist_maker.processor._get_sample_rate", return_value=44100)
    @patch("setlist_maker.processor.get_audio_duration")
    @patch("subprocess.run")
    def test_clamping_to_boundaries(self, mock_run, mock_duration, mock_rate):
        """Test that padding doesn't extend beyond file boundaries."""
        mock_duration.return_value = 25.0  # 5 chunks

        # Music starts at chunk 0
        rms = [-30.0] * 5
        mock_run.return_value = MagicMock(returncode=0, stderr=self._make_rms_stderr(rms))

        result = detect_content_boundaries(
            Path("/audio/test.wav"),
            threshold_db=-50.0,
            chunk_duration=5.0,
            required_consecutive=3,
            padding_seconds=10.0,  # Large padding
        )

        assert result.content_start == 0.0  # Clamped to 0
        assert result.content_end == 25.0  # Clamped to duration

    @patch("setlist_maker.processor._get_sample_rate", return_value=44100)
    @patch("setlist_maker.processor.get_audio_duration")
    @patch("subprocess.run")
    def test_short_file_reduces_consecutive(self, mock_run, mock_duration, mock_rate):
        """Test that very short files reduce the consecutive requirement."""
        mock_duration.return_value = 8.0  # Only ~1 chunk of 5s

        rms = [-30.0]
        mock_run.return_value = MagicMock(returncode=0, stderr=self._make_rms_stderr(rms))

        result = detect_content_boundaries(
            Path("/audio/test.wav"),
            threshold_db=-50.0,
            chunk_duration=5.0,
            required_consecutive=3,  # Would need 3, but only 1 possible
            padding_seconds=2.0,
        )

        # Should still detect content with reduced consecutive requirement
        # chunk 0: (0+1)*5 + 2 padding = 7.0, clamped to min(8.0, 7.0) = 7.0
        assert result.content_start == 0.0
        assert result.content_end == 7.0

    @patch("setlist_maker.processor._get_sample_rate", return_value=44100)
    @patch("setlist_maker.processor.get_audio_duration")
    @patch("subprocess.run")
    def test_ffmpeg_failure_returns_full_duration(self, mock_run, mock_duration, mock_rate):
        """Test that ffmpeg failure falls back to full duration."""
        mock_duration.return_value = 100.0
        mock_run.return_value = MagicMock(returncode=1, stderr="error")

        result = detect_content_boundaries(Path("/audio/test.wav"))

        assert result.content_start == 0.0
        assert result.content_end == 100.0

    @patch("setlist_maker.processor.get_audio_duration")
    def test_no_duration_returns_zero(self, mock_duration):
        """Test that missing duration returns zero boundaries."""
        mock_duration.return_value = None

        result = detect_content_boundaries(Path("/audio/test.wav"))

        assert result.content_start == 0.0
        assert result.content_end == 0.0
        assert result.total_duration == 0.0


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

    @patch("setlist_maker.processor.detect_content_boundaries")
    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_single_file_with_trim(self, mock_run, mock_check, mock_detect, temp_dir):
        """Test single file with trimming adds -ss and -t args."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_detect.return_value = ContentBoundaries(
            content_start=10.0, content_end=290.0, total_duration=300.0
        )

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        process_audio([input_file], output_file)

        # detect_content_boundaries should have been called
        mock_detect.assert_called_once()

        # Check the final ffmpeg command has -ss and -t
        call_args = mock_run.call_args[0][0]
        assert "-ss" in call_args
        ss_idx = call_args.index("-ss")
        assert call_args[ss_idx + 1] == "10.0"
        assert "-t" in call_args
        t_idx = call_args.index("-t")
        assert call_args[t_idx + 1] == "280.0"

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_single_file_no_trim(self, mock_run, mock_check, temp_dir):
        """Test single file without trimming has no -ss/-t."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        config = ProcessingConfig(trim_silence=False)
        process_audio([input_file], output_file, config=config)

        call_args = mock_run.call_args[0][0]
        assert "-ss" not in call_args
        assert "-t" not in call_args

    @patch("setlist_maker.processor.detect_content_boundaries")
    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_multiple_files_with_trim(self, mock_run, mock_check, mock_detect, temp_dir):
        """Test multi-file with trim: concats to WAV first, then encodes."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_detect.return_value = ContentBoundaries(
            content_start=5.0, content_end=95.0, total_duration=100.0
        )

        input1 = temp_dir / "input1.mp3"
        input2 = temp_dir / "input2.mp3"
        input1.write_bytes(b"dummy")
        input2.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        process_audio([input1, input2], output_file)

        # Should have 2 subprocess.run calls: concat WAV + final encode
        assert mock_run.call_count == 2

        # First call: concat to temp WAV
        first_call = mock_run.call_args_list[0][0][0]
        assert "-c:a" in first_call
        assert "pcm_s16le" in first_call

        # Second call: final encode with -ss and -t
        second_call = mock_run.call_args_list[1][0][0]
        assert "-ss" in second_call
        assert "-t" in second_call

    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_multiple_files_no_trim(self, mock_run, mock_check, temp_dir):
        """Test multi-file without trim uses direct concat."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        input1 = temp_dir / "input1.mp3"
        input2 = temp_dir / "input2.mp3"
        input1.write_bytes(b"dummy")
        input2.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        config = ProcessingConfig(trim_silence=False)
        process_audio([input1, input2], output_file, config=config)

        # Only 1 subprocess.run call (no temp concat)
        assert mock_run.call_count == 1
        call_args = mock_run.call_args[0][0]
        assert "-f" in call_args
        concat_idx = call_args.index("-f")
        assert call_args[concat_idx + 1] == "concat"

    @patch("setlist_maker.processor.detect_content_boundaries")
    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_applies_filter_chain(self, mock_run, mock_check, mock_detect, temp_dir):
        """Test that filter chain is applied."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_detect.return_value = ContentBoundaries(
            content_start=0.0, content_end=100.0, total_duration=100.0
        )

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
            trim_silence=False,
            apply_compression=False,
            apply_normalization=False,
        )

        process_audio([input_file], output_file, config=config)

        call_args = mock_run.call_args[0][0]
        assert "-af" not in call_args

    @patch("setlist_maker.processor.detect_content_boundaries")
    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_output_codec_settings(self, mock_run, mock_check, mock_detect, temp_dir):
        """Test that output uses correct codec and bitrate."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_detect.return_value = ContentBoundaries(
            content_start=0.0, content_end=100.0, total_duration=100.0
        )

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

    @patch("setlist_maker.processor.detect_content_boundaries")
    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_creates_output_directory(self, mock_run, mock_check, mock_detect, temp_dir):
        """Test that output directory is created if needed."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_detect.return_value = ContentBoundaries(
            content_start=0.0, content_end=100.0, total_duration=100.0
        )

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "subdir" / "nested" / "output.mp3"

        process_audio([input_file], output_file)

        assert output_file.parent.exists()

    @patch("setlist_maker.processor.detect_content_boundaries")
    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_raises_on_ffmpeg_failure(self, mock_run, mock_check, mock_detect, temp_dir):
        """Test raises FFmpegError when ffmpeg fails."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=1, stderr="Error: invalid input")
        mock_detect.return_value = ContentBoundaries(
            content_start=0.0, content_end=100.0, total_duration=100.0
        )

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        with pytest.raises(FFmpegError, match="FFmpeg processing failed"):
            process_audio([input_file], output_file)

    @patch("setlist_maker.processor.detect_content_boundaries")
    @patch("setlist_maker.processor.check_ffmpeg")
    @patch("subprocess.run")
    def test_returns_output_path(self, mock_run, mock_check, mock_detect, temp_dir):
        """Test that function returns the output path."""
        mock_check.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_detect.return_value = ContentBoundaries(
            content_start=0.0, content_end=100.0, total_duration=100.0
        )

        input_file = temp_dir / "input.mp3"
        input_file.write_bytes(b"dummy")
        output_file = temp_dir / "output.mp3"

        result = process_audio([input_file], output_file)

        assert result == output_file
