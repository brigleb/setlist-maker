# Processing Summary Report

## Summary

Add a post-processing summary to `setlist-maker process` that shows before/after loudness stats, duration change, and sparkline waveform visualizations.

## Output Format

```
──────────────────────────────────────────────────────────────
Processing Summary
──────────────────────────────────────────────────────────────

  Duration: 2h 1m 30s → 1h 58m 12s (trimmed 3m 18s)
  Size:     289.4 MB → 136.2 MB

  Loudness:
    Before:  -22.4 LUFS  │  -0.8 dBTP  │  LRA 14.2 LU
    After:   -16.0 LUFS  │  -1.5 dBTP  │  LRA 10.8 LU
                  +6.4         -0.7            -3.4

  Waveform (before):
  ▁▁▁▁▁▂▃▅▇▇▆▅▆▇▇▆▅▄▃▄▅▆▇▇▆▅▃▂▁▁▁▁▁▁▁▂▃▅▇▇▆▅▆▇▇▆▅▄▃▄▅▆▇▇▆▅▃▂▁▁

  Waveform (after):
  ▃▅▇▇▆▅▆▇▇▆▅▅▅▆▇▇▆▅▄▄▅▆▇▇▆▅▃▂▂▂▃▅▇▇▆▅▆▇▇▆▅▅▅▆▇▇▆▅▄▄▅▆▇▇▆▅▃▂▂
```

## Implementation

### processor.py — new analysis functions

- `analyze_loudness(audio_file) -> dict` — runs `ffmpeg -i file -af loudnorm=print_format=json -f null -`, parses JSON from stderr, returns `{input_i, input_tp, input_lra}`
- `analyze_waveform(audio_file, num_samples=60) -> list[float]` — runs ffmpeg with `astats` filter at calculated reset interval to get ~60 data points, returns normalized 0.0-1.0 RMS values

### cli.py — summary rendering

- `render_sparkline(values: list[float]) -> str` — maps 0.0-1.0 values to `▁▂▃▄▅▆▇█`
- `print_processing_summary(...)` — formats and prints the summary block
- `cmd_process()` updated to analyze input before processing, analyze output after, print summary

## Approach

Separate read-only ffmpeg analysis passes on input and output files. No changes to the processing pipeline itself. Analysis is best-effort — if it fails, processing still succeeds.

## Data Sources

- **Duration:** existing `get_audio_duration()` via ffprobe
- **Loudness:** FFmpeg `loudnorm` filter with `print_format=json` (outputs to stderr)
- **Waveform:** FFmpeg `astats` filter with `metadata=1:reset=N` for per-chunk RMS levels
