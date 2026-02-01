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
