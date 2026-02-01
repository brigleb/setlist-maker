"""
Waveform visualization for terminal display.

Renders audio segments as colored braille-pattern waveforms showing
peak amplitude, symmetric around a center line.
"""

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
