"""Tests for waveform visualization module."""


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
