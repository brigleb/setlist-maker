"""Tests for waveform visualization module."""

from unittest.mock import MagicMock


def test_amplitude_to_braille_silence():
    """Amplitude 0 returns empty braille character."""
    from setlist_maker.waveform import amplitude_to_braille

    result = amplitude_to_braille(0.0, 0.0)
    assert result == "⠀"  # U+2800 empty braille


def test_amplitude_to_braille_full():
    """Amplitude 1.0 returns full braille character."""
    from setlist_maker.waveform import amplitude_to_braille

    result = amplitude_to_braille(1.0, 1.0)
    assert result == "⣿"  # U+28FF all dots


def test_extract_peaks_mono():
    """Extract peaks from mono audio samples."""
    from setlist_maker.waveform import extract_peaks

    # Simulate audio samples: silence, medium, loud, medium, silence
    samples = [0, 0, 100, 100, 200, 200, 100, 100, 0, 0]
    peaks = extract_peaks(samples, num_buckets=5, channels=1)

    assert len(peaks) == 5
    assert peaks[0] == 0.0  # silence
    assert peaks[2] == 1.0  # loudest (normalized)
    assert peaks[4] == 0.0  # silence


def test_extract_peaks_stereo():
    """Extract peaks from stereo audio samples (interleaved)."""
    import pytest

    from setlist_maker.waveform import extract_peaks

    # Stereo: L, R, L, R, ... - average channels
    # Samples: [100, 100, 200, 200, 100, 100, 0, 0] (4 stereo frames)
    # After mono conversion: [100, 200, 100, 0]
    # Global max: 200
    # Bucket 0 (samples 0-1): [100, 200] -> peak=200 -> 200/200=1.0
    # Bucket 1 (samples 2-3): [100, 0] -> peak=100 -> 100/200=0.5
    samples = [100, 100, 200, 200, 100, 100, 0, 0]
    peaks = extract_peaks(samples, num_buckets=2, channels=2)

    assert len(peaks) == 2
    assert peaks[0] == 1.0  # loudest bucket
    assert peaks[1] == pytest.approx(0.5, abs=0.01)  # quieter bucket


def test_colorize_by_amplitude():
    """Colors vary by amplitude level."""
    from setlist_maker.waveform import colorize

    # Low amplitude = dim
    dim = colorize("⠀", 0.1)
    assert "\033[90m" in dim  # dim gray

    # High amplitude = bright cyan
    bright = colorize("⣿", 0.9)
    assert "\033[96m" in bright  # bright cyan

    # Both should have reset code
    assert "\033[0m" in dim
    assert "\033[0m" in bright


def test_colorize_disabled():
    """Color can be disabled."""
    from setlist_maker.waveform import colorize

    result = colorize("⣿", 1.0, use_color=False)
    assert result == "⣿"
    assert "\033[" not in result


def test_render_waveform_basic():
    """render_waveform produces braille output from AudioSegment."""
    from setlist_maker.waveform import render_waveform

    # Create mock AudioSegment
    mock_segment = MagicMock()
    mock_segment.channels = 1
    mock_segment.get_array_of_samples.return_value = [0, 50, 100, 150, 200, 150, 100, 50] * 100

    result = render_waveform(mock_segment, width=10, use_color=False)

    # Should be 10 braille characters
    assert len(result) == 10
    # All characters should be braille (U+2800 block)
    for char in result:
        assert 0x2800 <= ord(char) <= 0x28FF


def test_render_waveform_with_color():
    """render_waveform applies color codes."""
    from setlist_maker.waveform import render_waveform

    mock_segment = MagicMock()
    mock_segment.channels = 1
    mock_segment.get_array_of_samples.return_value = [100, 200, 100] * 100

    result = render_waveform(mock_segment, width=5, use_color=True)

    # Should contain ANSI codes
    assert "\033[" in result
    assert "\033[0m" in result
