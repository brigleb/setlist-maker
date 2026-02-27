# Interactive Stage Selection for `process` Command

## Summary

Add a Textual-based interactive stage picker to `setlist-maker process`. When no `--no-*` CLI flags override stages, a checkbox UI appears letting the user toggle processing stages before proceeding.

## Stages

All stages are fully optional and checked by default:

1. Concatenate files (only shown when multiple input files)
2. Remove leading silence
3. Apply compression
4. Normalize loudness (shows LUFS target)
5. Export MP3 (shows bitrate)

## Flow

1. CLI parses args, builds stage list (same as today)
2. If no `--no-*` flags passed, launch Textual stage picker
3. User toggles stages with Space, confirms with Enter, cancels with q/Escape
4. `ProcessingConfig` booleans updated from selection
5. Processing proceeds with selected stages only

## Implementation

- New file: `setlist_maker/stage_picker.py`
  - `StagePickerApp` — small Textual app with checkbox list
  - `run_stage_picker(stages) -> list[str] | None` — blocking entry point
- Modified: `setlist_maker/cli.py`
  - `cmd_process()` calls picker between stage list build and `process_audio()`
  - Cancelled picker exits cleanly
  - Selected stages update `ProcessingConfig` toggles

## Keybindings

- Space: toggle checkbox
- Enter: proceed with selection
- q / Escape: cancel
- Up/Down or j/k: navigate

## CLI Flag Behavior

If any `--no-compress`, `--no-normalize`, or `--no-silence-removal` is passed, skip the picker entirely — use flags directly (preserves current behavior, supports non-interactive/scripted use).
