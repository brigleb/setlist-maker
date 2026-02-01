# Terminal Audio Waveform Visualization

## Overview

Add a visual waveform preview when processing audio clips in setlist-maker. Each 30-second segment (and processed audio) displays as a single-row braille-pattern waveform showing peak amplitude, symmetric around a center line, with color intensity based on loudness.

## Visual Design

**Example output during identification:**
```
  [3/24] Sample at 1:30
  ⠀⠀⣀⣤⣶⣿⣿⣷⣦⣤⣀⠀⠀⣠⣴⣾⣿⣿⣿⣷⣦⣄⡀⠀⣀⣤⣶⣿⣿⣷⣤⣀⠀
  Found: Daft Punk - Around The World
```

**Specifications:**
- **Pattern:** Braille characters (U+2800 block) - 2×4 dot grid per character
- **Layout:** Symmetric/mirrored - top 2 rows show positive amplitude, bottom 2 rows mirror
- **Data:** Peak amplitude envelope (max absolute value per time bucket)
- **Width:** Fills terminal width minus padding (~70-110 characters)
- **Resolution:** 2 time samples per braille character

**Color gradient (ANSI):**
| Amplitude | Color | Code |
|-----------|-------|------|
| 0-25% | Dim gray | `\033[90m` |
| 25-50% | White | `\033[37m` |
| 50-75% | Cyan | `\033[36m` |
| 75-100% | Bright cyan | `\033[96m` |

## Technical Implementation

### New Module: `setlist_maker/waveform.py`

```python
def render_waveform(segment: AudioSegment, width: int | None = None) -> str:
    """
    Render an audio segment as a colored braille waveform string.

    Args:
        segment: pydub AudioSegment to visualize
        width: Target width in characters (default: terminal width - padding)

    Returns:
        Colored string ready to print
    """
```

### Data Flow

1. **Extract samples** - `segment.get_array_of_samples()` returns raw audio data
2. **Convert to mono** - Average channels if stereo
3. **Bucket samples** - Divide into N buckets where N = width × 2 (2 samples per braille char)
4. **Calculate peaks** - Max absolute amplitude per bucket
5. **Normalize** - Scale to 0.0-1.0 based on segment's max amplitude
6. **Map to braille** - Convert pairs of values to braille characters with symmetric dots
7. **Apply color** - Wrap in ANSI codes based on amplitude

### Braille Mapping

Braille Unicode (U+2800 base) with dot positions:
```
[1] [4]    bits: 0x01  0x08
[2] [5]          0x02  0x10
[3] [6]          0x04  0x20
[7] [8]          0x40  0x80
```

For symmetric waveform around center:
- Amplitude 0: empty (⠀)
- Amplitude ~50%: dots 2,5 and 3,6 (middle rows)
- Amplitude 100%: all 8 dots (⣿)

Each character encodes 2 time samples (left column = sample 1, right column = sample 2).

### Fallback

If terminal doesn't support Unicode (`$TERM` check), fall back to ASCII:
```
  [3/24] Sample at 1:30
  __##^^^^##__##^^^^^##__##^^^^##__
```

## Integration Points

### 1. Identification (`cli.py` ~line 401)

In `process_single_file()`, after slicing and before Shazam call:

```python
from setlist_maker.waveform import render_waveform

for i, (timestamp, segment) in enumerate(slices[start_index:], start_index + 1):
    time_str = format_timestamp(timestamp)
    waveform = render_waveform(segment)
    print(f"  [{i}/{total_slices}] Sample at {time_str}")
    print(f"  {waveform}")
    # ... identification continues
```

### 2. Audio Processing (`processor.py`)

After each processing stage, show waveform of a sample from the output:

```python
# After normalization completes
preview = AudioSegment.from_file(output_path)[:30000]  # First 30 sec
print(f"  {render_waveform(preview)}")
```

### 3. File Structure

```
setlist_maker/
├── __init__.py
├── cli.py          # Modified: import and call render_waveform
├── editor.py
├── processor.py    # Modified: show waveform after processing stages
└── waveform.py     # NEW
```

## Dependencies

No new dependencies required:
- `pydub` (existing) - provides `AudioSegment.get_array_of_samples()`
- `shutil` (stdlib) - provides `get_terminal_size()`
- `os` (stdlib) - provides `$TERM` check

## Testing

- Unit tests for braille mapping function
- Unit tests for amplitude normalization
- Integration test with sample audio file
- Verify fallback behavior when `$TERM=dumb`
