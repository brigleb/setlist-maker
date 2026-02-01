"""
Waveform visualization for terminal display.

Renders audio segments as colored braille-pattern waveforms showing
peak amplitude, symmetric around a center line.
"""

from __future__ import annotations

import array
import os
import shutil

# Braille Unicode block starts at U+2800
# Dot positions and their bit values:
#   [1] [4]    0x01  0x08
#   [2] [5]    0x02  0x10
#   [3] [6]    0x04  0x20
#   [7] [8]    0x40  0x80

BRAILLE_BASE = 0x2800

# Symmetric dot patterns for amplitude levels (0-2 per column)
# Level 0: no dots, Level 1: middle dots, Level 2: all 4 dots
LEFT_COLUMN_DOTS = [
    0x00,  # level 0: no dots
    0x02 | 0x40,  # level 1: dots 2,7 (middle)
    0x01 | 0x02 | 0x04 | 0x40,  # level 2: dots 1,2,3,7
]

RIGHT_COLUMN_DOTS = [
    0x00,  # level 0: no dots
    0x10 | 0x80,  # level 1: dots 5,8 (middle)
    0x08 | 0x10 | 0x20 | 0x80,  # level 2: dots 4,5,6,8
]

# ASCII characters for fallback: silence, low, medium, high
ASCII_CHARS = "_-#^"


def amplitude_to_braille(amp_left: float, amp_right: float) -> str:
    """
    Convert two amplitude values (0.0-1.0) to a single braille character.

    Each column of the braille character represents one amplitude value.
    The pattern is symmetric around the center for a waveform look.

    Args:
        amp_left: Amplitude for left column (0.0-1.0)
        amp_right: Amplitude for right column (0.0-1.0)

    Returns:
        Single braille character representing both amplitudes
    """
    # Map amplitude to level (0, 1, or 2)
    level_left = min(2, int(amp_left * 3))
    level_right = min(2, int(amp_right * 3))

    # Combine dot patterns
    dots = LEFT_COLUMN_DOTS[level_left] | RIGHT_COLUMN_DOTS[level_right]

    return chr(BRAILLE_BASE + dots)


def amplitude_to_ascii(amp_left: float, amp_right: float) -> str:
    """
    Convert two amplitude values to ASCII characters (fallback).

    Args:
        amp_left: Amplitude for first character (0.0-1.0)
        amp_right: Amplitude for second character (0.0-1.0)

    Returns:
        Two ASCII characters representing amplitudes
    """
    idx_left = min(3, int(amp_left * 4))
    idx_right = min(3, int(amp_right * 4))
    return ASCII_CHARS[idx_left] + ASCII_CHARS[idx_right]


def extract_peaks(
    samples: list[int] | array.array,
    num_buckets: int,
    channels: int = 1,
) -> list[float]:
    """
    Extract peak amplitudes from audio samples.

    Divides samples into buckets and finds the maximum absolute
    amplitude in each bucket, normalized to 0.0-1.0.

    Args:
        samples: Raw audio samples (interleaved if stereo)
        num_buckets: Number of output values (typically terminal width * 2)
        channels: Number of audio channels (1=mono, 2=stereo)

    Returns:
        List of normalized peak amplitudes (0.0-1.0)
    """
    if not samples:
        return [0.0] * num_buckets

    # Convert stereo to mono by averaging channels
    if channels == 2:
        mono_samples = []
        for i in range(0, len(samples) - 1, 2):
            mono_samples.append((samples[i] + samples[i + 1]) // 2)
        samples = mono_samples

    # Find global max for normalization
    max_amp = max(abs(s) for s in samples) if samples else 1
    if max_amp == 0:
        return [0.0] * num_buckets

    # Divide into buckets and find peak in each
    samples_per_bucket = max(1, len(samples) // num_buckets)
    peaks = []

    for i in range(num_buckets):
        start = i * samples_per_bucket
        end = start + samples_per_bucket
        bucket = samples[start:end] if start < len(samples) else [0]

        if bucket:
            peak = max(abs(s) for s in bucket)
            peaks.append(peak / max_amp)
        else:
            peaks.append(0.0)

    return peaks


# ANSI color codes for amplitude gradient
COLORS = [
    "\033[90m",  # 0-25%: dim gray
    "\033[37m",  # 25-50%: white
    "\033[36m",  # 50-75%: cyan
    "\033[96m",  # 75-100%: bright cyan
]
RESET = "\033[0m"


def colorize(char: str, amplitude: float, use_color: bool = True) -> str:
    """
    Wrap a character in ANSI color codes based on amplitude.

    Args:
        char: Character to colorize
        amplitude: Amplitude value (0.0-1.0) to determine color
        use_color: If False, return char unchanged

    Returns:
        Character wrapped in ANSI codes, or plain char if use_color=False
    """
    if not use_color:
        return char

    # Map amplitude to color index (0-3)
    color_idx = min(3, int(amplitude * 4))
    return f"{COLORS[color_idx]}{char}{RESET}"


def get_terminal_width() -> int:
    """Get terminal width, defaulting to 80 if unavailable."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def supports_unicode() -> bool:
    """Check if terminal likely supports Unicode braille characters."""
    term = os.environ.get("TERM", "")
    # Most modern terminals support Unicode; dumb terminals don't
    return term != "dumb"


def render_waveform(
    segment,  # AudioSegment type but avoid import for flexibility
    width: int | None = None,
    use_color: bool | None = None,
) -> str:
    """
    Render an audio segment as a colored braille waveform string.

    Falls back to ASCII characters when Unicode is not supported.

    Args:
        segment: pydub AudioSegment to visualize
        width: Target width in characters (default: terminal width - 10)
        use_color: Enable ANSI colors (default: auto-detect)

    Returns:
        Colored string ready to print
    """
    # Determine width
    if width is None:
        width = get_terminal_width() - 10  # Leave padding

    # Check Unicode support
    unicode_ok = supports_unicode()

    # Auto-detect color support (only with Unicode)
    if use_color is None:
        use_color = unicode_ok

    # Extract samples
    samples = segment.get_array_of_samples()
    channels = segment.channels

    # Need 2 samples per braille character
    num_buckets = width * 2
    peaks = extract_peaks(samples, num_buckets, channels)

    # Build waveform string
    chars = []
    for i in range(0, len(peaks) - 1, 2):
        amp_left = peaks[i]
        amp_right = peaks[i + 1] if i + 1 < len(peaks) else 0.0
        avg_amp = (amp_left + amp_right) / 2

        if unicode_ok:
            char = amplitude_to_braille(amp_left, amp_right)
            colored = colorize(char, avg_amp, use_color)
            chars.append(colored)
        else:
            # ASCII fallback - 2 chars per position
            chars.append(amplitude_to_ascii(amp_left, amp_right))

    return "".join(chars)
