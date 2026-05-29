# Web editor: prominent, column-aligned, editable set description

**Issue:** #14
**Date:** 2026-05-29

## Problem

The set description (the Claude-generated `Tracklist.summary`) renders at the top
of the web editor as small, muted, italic, read-only text (`.summary` in
`web_editor.html`). It runs edge-to-edge rather than aligning with the 760px
track-list column below it, and can't be edited.

## Goal

Make the description prominent, aligned to the track-list column, and editable in
the browser — persisting on Save and surviving a reopen via the existing markdown
round-trip.

## Decisions (settled during brainstorming)

- **Edit UX:** an always-on `<textarea>` styled to look like prose (not a
  click-to-edit toggle, not `contenteditable`). Most discoverable for a large text
  block and plaintext-safe.
- **Empty state:** included. The textarea is always rendered; when there's no
  summary it shows a placeholder so one can be written and saved.

## Front-end (`web_editor.html`)

Replace `<div class="summary" id="summary" hidden>` with an always-visible
`<textarea id="summary">`.

**Layout / styling**
- Reads like prose: transparent background, no border at rest, larger (~16px)
  font, normal dark color (e.g. `#3a3a3c`), not italic.
- Constrained to `max-width:760px; margin:0 auto` with `20px` horizontal padding
  so its text lines up with the track rows in `<main>`.
- On focus: a subtle outline/border using the same accent cue the editing rows
  use, to signal editability.
- Auto-grows to fit content (set height to `scrollHeight` on load and on input)
  so the full description shows without an inner scrollbar.
- Placeholder `Add a description for this set…` when empty — this *is* the
  empty-state affordance.

**Behavior**
- `load()` sets `summary.value = data.summary || ""` and sizes it.
- `oninput` → `setDirty(true)` (reuses the existing dirty flag / Save enablement).
- Save payload gains a top-level `summary: summaryEl.value`. On success it's
  persisted server-side; no reload needed unless new tracks exist (today's
  behavior).
- Plain text only via `.value` — preserves the page's deliberate no-`innerHTML`
  XSS posture.

## Back-end (`web_editor.py`)

Wire the summary through the existing pure helper rather than handling it
separately in the request handler, so all edits are applied in one tested place.

`apply_edits(tracklist, edits, corrections_db, summary=_UNSET)`:
- Module-level `_UNSET` sentinel: summary absent from payload → leave
  `tracklist.summary` unchanged. Keeps the three existing callers/tests working
  untouched.
- A provided value is **whitespace-normalized to a single paragraph** (collapse
  newline/whitespace runs to single spaces); empty/whitespace-only → `None`
  (clears it). Normalization makes the round-trip lossless, because
  `parse_markdown_tracklist()` already joins prose lines with spaces — so what you
  save is exactly what reloads.

`_handle_save()` reads `payload.get("summary")` and passes it to `apply_edits`.

`tracklist_to_api()` already emits `summary` — no change.

## Data flow

```
textarea
  → POST /api/save {tracks, summary}
  → apply_edits(..., summary=...) sets tracklist.summary
  → save_tracklist() writes .md / .json
  → reopen: parse_markdown_tracklist() reads .md
  → tracklist_to_api() → textarea
```

## Testing (TDD, matching existing test style in `tests/test_web_editor.py`)

Pure helper:
- `apply_edits` sets `tracklist.summary` from a provided value.
- clears to `None` on empty/whitespace-only.
- leaves `tracklist.summary` unchanged when the arg is omitted (sentinel).
- normalizes internal newlines/whitespace runs to a single-paragraph string.

Round-trip:
- `apply_edits(summary=...)` → `to_markdown()` → `parse_markdown_tracklist()`
  preserves the (normalized) summary.

HTTP integration:
- `POST /api/save` with `summary` in the body writes it into the `.md` file.

Page asset:
- HTML contains `<textarea id="summary"`, the placeholder text, and references
  `summary` in the save-payload construction.

## Out of scope

- Multi-paragraph / rich-text descriptions (summary is one paragraph by design).
- Regenerating the summary from within the editor.
