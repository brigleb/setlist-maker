# Waveform Visualization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add colored braille-pattern waveform visualization when processing audio clips

**Architecture:** New `waveform.py` module with pure functions for sample extraction, braille mapping, and ANSI coloring. Integrated into `cli.py` (identification loop) and `processor.py` (after processing completes).

**Tech Stack:** pydub (existing), shutil/os (stdlib), Unicode braille block (U+2800)

---

### Task 1: Create waveform module with braille mapping

**Files:**
- Create: `setlist_maker/waveform.py`
- Create: `tests/test_waveform.py`

**Step 1: Write the failing test for amplitude_to_braille**

```python
"""Tests for waveform visualization module."""

import pytest


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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_waveform.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'setlist_maker.waveform'"

**Step 3: Write minimal implementation**

```python
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
    0x00,        # level 0: no dots
    0x02 | 0x40, # level 1: dots 2,7 (middle)
    0x01 | 0x02 | 0x04 | 0x40,  # level 2: dots 1,2,3,7
]

RIGHT_COLUMN_DOTS = [
    0x00,        # level 0: no dots
    0x10 | 0x80, # level 1: dots 5,8 (middle)
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
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_waveform.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add setlist_maker/waveform.py tests/test_waveform.py
git commit -m "feat(waveform): add braille amplitude mapping"
```

---

### Task 2: Add sample extraction and peak calculation

**Files:**
- Modify: `setlist_maker/waveform.py`
- Modify: `tests/test_waveform.py`

**Step 1: Write the failing test for extract_peaks**

Add to `tests/test_waveform.py`:

```python
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
    from setlist_maker.waveform import extract_peaks

    # Stereo: L, R, L, R, ... - average channels
    samples = [100, 100, 200, 200, 100, 100, 0, 0]  # 4 stereo samples
    peaks = extract_peaks(samples, num_buckets=2, channels=2)

    assert len(peaks) == 2
    assert peaks[0] == 1.0  # loudest bucket
    assert peaks[1] == pytest.approx(0.25, abs=0.1)  # quieter bucket
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_waveform.py::test_extract_peaks_mono -v`
Expected: FAIL with "ImportError: cannot import name 'extract_peaks'"

**Step 3: Write minimal implementation**

Add to `setlist_maker/waveform.py`:

```python
def extract_peaks(
    samples: list[int] | "array.array",
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
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_waveform.py -v -k extract_peaks`
Expected: PASS

**Step 5: Commit**

```bash
git add setlist_maker/waveform.py tests/test_waveform.py
git commit -m "feat(waveform): add peak extraction from audio samples"
```

---

### Task 3: Add ANSI color support

**Files:**
- Modify: `setlist_maker/waveform.py`
- Modify: `tests/test_waveform.py`

**Step 1: Write the failing test for colorize**

Add to `tests/test_waveform.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_waveform.py::test_colorize_by_amplitude -v`
Expected: FAIL with "ImportError: cannot import name 'colorize'"

**Step 3: Write minimal implementation**

Add to `setlist_maker/waveform.py`:

```python
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
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_waveform.py -v -k colorize`
Expected: PASS

**Step 5: Commit**

```bash
git add setlist_maker/waveform.py tests/test_waveform.py
git commit -m "feat(waveform): add ANSI color support"
```

---

### Task 4: Add main render_waveform function

**Files:**
- Modify: `setlist_maker/waveform.py`
- Modify: `tests/test_waveform.py`

**Step 1: Write the failing test for render_waveform**

Add to `tests/test_waveform.py`:

```python
from unittest.mock import MagicMock


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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_waveform.py::test_render_waveform_basic -v`
Expected: FAIL with "ImportError: cannot import name 'render_waveform'"

**Step 3: Write minimal implementation**

Add to `setlist_maker/waveform.py`:

```python
import os
import shutil

from pydub import AudioSegment


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
    segment: AudioSegment,
    width: int | None = None,
    use_color: bool | None = None,
) -> str:
    """
    Render an audio segment as a colored braille waveform string.

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

    # Auto-detect color support
    if use_color is None:
        use_color = supports_unicode()

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

        char = amplitude_to_braille(amp_left, amp_right)
        colored = colorize(char, avg_amp, use_color)
        chars.append(colored)

    return "".join(chars)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_waveform.py -v -k render_waveform`
Expected: PASS

**Step 5: Commit**

```bash
git add setlist_maker/waveform.py tests/test_waveform.py
git commit -m "feat(waveform): add main render_waveform function"
```

---

### Task 5: Integrate waveform into identification loop

**Files:**
- Modify: `setlist_maker/cli.py:401-419`

**Step 1: Read current code to understand context**

The identification loop is in `process_single_file()` around line 401. Currently:
```python
for i, (timestamp, segment) in enumerate(slices[start_index:], start_index + 1):
    time_str = format_timestamp(timestamp)
    print(f"  [{i}/{total_slices}] Sample at {time_str}...", end=" ", flush=True)

    track_info = await identify_sample_with_retry(shazam, segment, temp_dir)

    if track_info:
        print(f"Found: {track_info['artist']} - {track_info['title']}")
    else:
        print("Not identified")
```

**Step 2: Add import at top of cli.py**

Add after line 77 (after processor imports):

```python
from setlist_maker.waveform import render_waveform
```

**Step 3: Modify the identification loop**

Replace lines 401-410 with:

```python
for i, (timestamp, segment) in enumerate(slices[start_index:], start_index + 1):
    time_str = format_timestamp(timestamp)
    waveform = render_waveform(segment)
    print(f"  [{i}/{total_slices}] Sample at {time_str}")
    print(f"  {waveform}")

    track_info = await identify_sample_with_retry(shazam, segment, temp_dir)

    if track_info:
        print(f"  Found: {track_info['artist']} - {track_info['title']}")
    else:
        print("  Not identified")
```

**Step 4: Run existing CLI tests**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (no behavior change, just output formatting)

**Step 5: Commit**

```bash
git add setlist_maker/cli.py
git commit -m "feat(cli): show waveform during track identification"
```

---

### Task 6: Integrate waveform into audio processing

**Files:**
- Modify: `setlist_maker/cli.py:584-600`

**Step 1: Understand current processing output**

In `cmd_process()`, after `process_audio()` completes (around line 591):
```python
result_path = process_audio(...)
print(f"\n✓ Output saved: {result_path}")

# Show output file info
output_duration = get_audio_duration(result_path)
```

**Step 2: Add waveform preview after processing**

After line 591 (`print(f"\n✓ Output saved: {result_path}")`), add:

```python
        # Show waveform preview of processed audio
        try:
            from pydub import AudioSegment as PydubSegment

            preview = PydubSegment.from_file(str(result_path))[:30000]  # First 30 sec
            waveform = render_waveform(preview)
            print(f"  {waveform}")
        except Exception:
            pass  # Skip waveform if preview fails
```

**Step 3: Run processor tests**

Run: `pytest tests/test_processor.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add setlist_maker/cli.py
git commit -m "feat(cli): show waveform preview after audio processing"
```

---

### Task 7: Add ASCII fallback for dumb terminals

**Files:**
- Modify: `setlist_maker/waveform.py`
- Modify: `tests/test_waveform.py`

**Step 1: Write the failing test for ASCII fallback**

Add to `tests/test_waveform.py`:

```python
def test_amplitude_to_ascii():
    """ASCII fallback for terminals without Unicode."""
    from setlist_maker.waveform import amplitude_to_ascii

    assert amplitude_to_ascii(0.0, 0.0) == "__"  # silence
    assert amplitude_to_ascii(0.5, 0.5) == "##"  # medium
    assert amplitude_to_ascii(1.0, 1.0) == "^^"  # loud


def test_render_waveform_ascii_fallback(monkeypatch):
    """render_waveform uses ASCII when Unicode not supported."""
    from setlist_maker.waveform import render_waveform

    monkeypatch.setenv("TERM", "dumb")

    mock_segment = MagicMock()
    mock_segment.channels = 1
    mock_segment.get_array_of_samples.return_value = [0, 100, 200, 100, 0] * 100

    result = render_waveform(mock_segment, width=5, use_color=False)

    # Should be ASCII characters only
    assert all(ord(c) < 128 for c in result)
    assert any(c in "_#^" for c in result)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_waveform.py::test_amplitude_to_ascii -v`
Expected: FAIL with "ImportError: cannot import name 'amplitude_to_ascii'"

**Step 3: Write minimal implementation**

Add to `setlist_maker/waveform.py`:

```python
# ASCII characters for fallback: silence, low, medium, high
ASCII_CHARS = "_-#^"


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
```

**Step 4: Update render_waveform to use fallback**

Modify `render_waveform()` to check for Unicode support:

```python
def render_waveform(
    segment: AudioSegment,
    width: int | None = None,
    use_color: bool | None = None,
) -> str:
    """
    Render an audio segment as a colored braille waveform string.

    Args:
        segment: pydub AudioSegment to visualize
        width: Target width in characters (default: terminal width - 10)
        use_color: Enable ANSI colors (default: auto-detect)

    Returns:
        Colored string ready to print
    """
    # Determine width
    if width is None:
        width = get_terminal_width() - 10

    # Check Unicode support
    unicode_ok = supports_unicode()

    # Auto-detect color support (only with Unicode)
    if use_color is None:
        use_color = unicode_ok

    # Extract samples
    samples = segment.get_array_of_samples()
    channels = segment.channels

    # Need 2 samples per character (braille) or per 2 chars (ASCII)
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
```

**Step 5: Run test to verify it passes**

Run: `pytest tests/test_waveform.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add setlist_maker/waveform.py tests/test_waveform.py
git commit -m "feat(waveform): add ASCII fallback for dumb terminals"
```

---

### Task 8: Manual integration test

**Step 1: Create a short test audio file**

```bash
# Generate 5 seconds of test tone with ffmpeg
ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -c:a pcm_s16le /tmp/test_tone.wav
```

**Step 2: Test identification with waveform**

```bash
cd /home/ray/bin/setlist-maker
setlist-maker identify /tmp/test_tone.wav
```

Expected: Should see waveform visualization for each 30-second slice (will be one slice for 5-second file).

**Step 3: Test processing with waveform**

```bash
setlist-maker process /tmp/test_tone.wav -o /tmp/processed.mp3
```

Expected: Should see waveform preview after "Output saved" message.

**Step 4: Test ASCII fallback**

```bash
TERM=dumb setlist-maker identify /tmp/test_tone.wav
```

Expected: Should see ASCII characters (`_-#^`) instead of braille.

**Step 5: Clean up test files**

```bash
rm /tmp/test_tone.wav /tmp/processed.mp3
```

---

### Task 9: Final commit and summary

**Step 1: Run full test suite**

```bash
pytest -v
ruff check .
ruff format --check .
```

Expected: All tests pass, no lint errors.

**Step 2: Final commit if any uncommitted changes**

```bash
git status
# If any changes:
git add -A
git commit -m "chore: cleanup after waveform implementation"
```

**Step 3: Summary of changes**

Files created:
- `setlist_maker/waveform.py` - Waveform rendering module
- `tests/test_waveform.py` - Unit tests

Files modified:
- `setlist_maker/cli.py` - Integration points for identification and processing

New features:
- Braille waveform visualization during track identification
- Waveform preview after audio processing
- Color gradient based on amplitude
- ASCII fallback for dumb terminals
