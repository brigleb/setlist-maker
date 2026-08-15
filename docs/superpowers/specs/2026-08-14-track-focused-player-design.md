# Track-Focused Player — Design

**Status:** approved
**Date:** 2026-08-14
**Component:** `setlist_maker/web_editor.html` (player bar)

## Problem

The web editor's player is a single global scrubber spanning the whole
recording. Measured on a real 4-hour set (`2026-07-22-Keys-Lounge.mp3`,
14,416 s) the seek input is 1062 px wide:

```
14,416 s / 1062 px  =  13.6 seconds per pixel
```

One pixel of drag moves playback nearly 14 seconds. There is no precise
position on that bar — fine positioning is not merely awkward, it is
unavailable. A request for "skip ±15 s" is the symptom; the absent
resolution is the cause.

Three further gaps found in the same pass:

- `playingIndex` is assigned in `playFrom()` and **never read**. No row is
  highlighted and nothing scrolls into view, so with 56 tracks the player
  reports one track while the visible list shows another.
- The only time readout is set-relative (`1:06 / 4:00:16`). The question the
  editor exists to answer — "does this track really start here?" — needs
  position *within* the track, which the UI never shows.
- The player has no keyboard shortcuts, in a review loop that is otherwise
  keyboard-driven.

## Framing

This is not a music player. It is a verification tool. The loop is: jump to
track N, listen briefly, confirm or correct, move on — 56 times. Every
control is judged by how well it serves that loop.

## Design

### The window

Each track owns a **window**: from its own `timestamp` to the *next* track's
`timestamp`. The final track's window ends at the audio duration.

The scrubber spans the current window rather than the whole set. For a
4-minute track that is 1062 px / 240 s = **0.23 s/px**, a ~59× improvement on
today's 13.6 s/px.

Windows are derived from the live `tracks` array on each use rather than
cached, so they follow edits with no separate bookkeeping. The only edit that
moves a boundary today is inserting a row: `rowEl`'s editor exposes a
timestamp field for inserted rows only (`if (isNew)`), and committing it
re-sorts `tracks` in place. Existing rows edit artist and title only, so
their boundaries are stable for the life of the page.

The window is therefore defined by list order: the page renders `tracks` in
chronological order and `apply_edits` preserves that order server-side, so
`tracks[i+1].timestamp` is the next boundary. A window is clamped to a
minimum of 1 s, so two tracks sharing a timestamp cannot produce a
zero-width scrubber and a division by zero.

### Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ┌────┐  ⏮  ↺15  ⏸  15↻  ⏭   Link Wray — La De Da          track 1 of 56 │
│ │art │  ├────●──────────────────────────────────────┤                    │
│ └────┘  0:43 in · 3:17 left                     9:13 into set · 4:00:16  │
└──────────────────────────────────────────────────────────────────────────┘
```

- Artwork (48 px) and the transport group anchor the left edge.
- The scrubber occupies the full remaining width beneath them.
- Track-relative timing sits left, under the scrubber, where the eye already
  is. Set-relative timing is parked at the far right — available, secondary.
- Track position (`track 1 of 56`) sits top-right.
- Target height ~84 px (today: 66 px). `body { padding-bottom }` must be
  updated to match, or the final list row hides behind the bar.

### Behavior decisions

**Playback continues past the window edge.** When `currentTime` passes into
the next track's window the player re-scopes: new label, new artwork, new
window, new highlighted row. Hearing the transition is precisely how a
boundary gets verified, so stopping at the edge would remove the thing being
checked.

**±15 s does not clamp to the window.** Nudging back from 5 s into a track
crosses into the previous track's tail and re-scopes. Same reasoning: "is
this boundary late?" is answered by crossing it. Seeks clamp only to the
audio's real bounds, `[0, duration]`.

**⏮ / ⏭ jump to track starts**, including rejected tracks — a track is often
auditioned *before* the decision to un-reject it. ⏮ from more than 3 s into a
track returns to that track's own start (the familiar transport convention);
within the first 3 s it goes to the previous track.

### Controls and keyboard

| Control | Action |
|---------|--------|
| ⏮ | Previous track start (or restart current, see above) |
| ↺15 | `currentTime -= 15` |
| ▶ / ⏸ | Play / pause |
| 15↻ | `currentTime += 15` |
| ⏭ | Next track start |

Keyboard: `Space` play/pause, `←`/`→` ∓15 s, `↑`/`↓` previous/next track.

All shortcuts are suppressed when focus is inside an `input` or `textarea`,
so editing an artist, title, timestamp or the set description behaves
normally. This extends the existing `keydown` listener (which today handles
only `Escape` for the artwork overlay) rather than adding a second one.

### Playing-row highlight

`playingIndex` becomes load-bearing. The row for the current window gets a
`.playing` class and is scrolled into view when the player advances to a
track the user did not click.

Scroll-into-view fires only on *automatic* advance, not when the user clicks
a row's ▶ — that row is already under their cursor, and yanking the list
would be hostile.

The highlight must survive `render()`, which rebuilds every row.

## Out of scope

- Track-boundary tick marks on a set-wide strip (the "two-tier" option).
  Deferred; the track window solves the precision problem on its own.
- Waveform display.
- Playback rate control.
- A JS test harness (see below).

## Testing

`web_editor.html` has no JavaScript tests. The Python suite asserts only that
substrings appear in the file — the exact gap that allowed a blank editor, a
stale thumbnail, and a corner-cropped image to ship with a green suite
earlier in this project.

**The gate for this work is the browser, not pytest.** Verification:

1. **Window maths, asserted in the live page.** With the real 56-track set
   loaded, execute assertions in the page context: every window start equals
   its track's timestamp; every window end equals the next track's timestamp;
   the last ends at `audio.duration`; windows are contiguous and non-zero.
2. **Visual confirmation** of the layout at the stated height, with no list
   row obscured by the bar.
3. **Each control exercised** and its effect on `audio.currentTime` observed.
4. **Highlight tracking**: confirm the highlighted row follows automatic
   advance across a boundary, and that clicking a row's ▶ does not scroll.
5. **Keyboard suppression**: type a space into an artist field and confirm
   playback does not toggle.

Python-side tests remain worth adding for what they can actually catch:
that the new element IDs exist in the served HTML, and that the overlay
ordering guard (`#artwork-overlay` before `<script>`) still holds.

**This feature will carry weaker regression protection than the Python
code.** A JS harness (node plus a runner) is a large addition to a
pure-Python project for one HTML file and is not proposed here. Accepted
knowingly, recorded here so the tradeoff is not rediscovered later.

## Files

- `setlist_maker/web_editor.html` — all player markup, styles, and script
- `tests/test_web_editor.py` — element-presence assertions only
- `CLAUDE.md` — player behavior notes in the web-editor section
- `README.md` — browser-editor section, if controls are user-visible enough
  to document (they are: the keyboard shortcuts)
