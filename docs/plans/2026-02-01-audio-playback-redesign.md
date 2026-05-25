# Audio Playback Redesign

## Problem

The current editor audio playback has several issues:
- UI locks up during playback due to sounddevice callback conflicts with Textual's event loop
- Audio is glitchy
- Waveform doesn't display reliably
- Waveform hides the info bar instead of being additive

## Solution

Replace `sounddevice` callback-based streaming with subprocess-based playback.

### Architecture

1. **Segment extraction**: Use ffmpeg to extract 30-second segment to temp file (~100ms)
2. **Playback**: Spawn `afplay` (macOS) or `ffplay` (cross-platform) as subprocess
3. **Position tracking**: Track via `time.monotonic()` elapsed since start—no callbacks needed
4. **Waveform**: Extract peaks via pydub (fast sample read), display with playhead indicator

### UI Layout

```
┌─────────────────────────────────────────────────────────────┐
│ ▶ 1:30  ⣿⣿⣿⣿⣷⣶⣤⣀⣠⣤⣶⣷⣿⣿⣿⣿⣷⣶⣤⣀⣠⣤⣶⣷⣿⣿  12s/30s │  ← waveform (only when playing)
├─────────────────────────────────────────────────────────────┤
│ File: set.mp3  │  Tracks: 24  │  Edited: 3                 │  ← info-bar (always visible)
├─────────────────────────────────────────────────────────────┤
│  #  │  Time   │  Artist              │  Title              │  ← DataTable
└─────────────────────────────────────────────────────────────┘
```

- Waveform bar appears at top when playing, pushes content down
- Bright cyan = played portion, dim = upcoming
- Navigating to different track stops playback

### Implementation

**PlaybackEngine changes:**
- Remove sounddevice dependency for playback
- Use `subprocess.Popen` for audio player
- Use `tempfile` for segment extraction
- Position = `(time.monotonic() - start_time) / duration`

**Player detection order:**
1. `afplay` (macOS built-in)
2. `ffplay -nodisp -autoexit` (cross-platform)

**ffmpeg extraction command:**
```bash
ffmpeg -ss <start> -t 30 -i input.mp3 -c copy /tmp/segment.mp3
```

## Tasks

1. Rewrite `PlaybackEngine` to use subprocess-based playback
2. Update CSS so waveform bar is additive (doesn't hide info bar)
3. Stop playback when navigating to different track
4. Clean up temp files properly
