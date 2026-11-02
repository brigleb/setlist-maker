// The page's keyboard map.

import { S } from "./state.js";
import { audio, playerEl, pplayEl, toast } from "./dom.js";
import { keptTrackAt } from "./model.js";
import { redo, undo } from "./history.js";
import { SKIP_SECONDS, nextTrack, prevTrack, skip } from "./player.js";
import { moveStart, splitAt, startOf } from "./edits.js";
import { identifyAt } from "./inspector.js";
import { overlay } from "./artwork.js";

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && overlay.classList.contains("open")) { overlay.click(); return; }
  // The artwork panel owns the keyboard while it is open: its candidate tiles
  // are BUTTONs, and the guard below exempts only inputs, so the arrows would
  // seek the audio while the user is browsing covers.
  if (overlay.classList.contains("open")) return;
  // Never steal keys from an editor: artist/title/time fields and the summary
  // textarea all need Space and the arrows to behave normally. #seek is an
  // INPUT too, but deliberately excluded -- e.preventDefault() below already
  // blocks its native arrow-key stepping, so arrows should seek rather than
  // get eaten here just because the scrubber has focus.
  // tagName is enough because this page has no contenteditable surface (and
  // no shadow root that could hide one behind its host). Anyone adding a
  // rich-text field must extend this guard (e.g. el.isContentEditable), or
  // Space and the arrows will be eaten while they type.
  const el = document.activeElement;
  if (el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && el.type !== "range"))) return;
  // Past the guard above, so ⌘Z inside a text field stays the field's own undo.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "z") {
    e.preventDefault();
    if (e.shiftKey) redo(); else undo();
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  // [ and ] nudge the selected track's start by a second; with ⇧ (which makes
  // them { and }) by five. Above the player guard: no audio is needed.
  const nudge = { "[": -1, "]": 1, "{": -5, "}": 5 }[e.key];
  if (nudge !== undefined) {
    const t = S.selectedTrack;
    if (!t || S.liveMode) return;
    e.preventDefault();
    if (!moveStart(t, startOf(t) + nudge)) toast("It can't move any further: it would pass the track next to it");
    return;
  }
  if (playerEl.classList.contains("disabled")) return;
  switch (e.key) {
    case " ":
      // Space normally activates a focused button; only #pplay should
      // intercept it here, so Tab-to-Save-and-press-Space still saves.
      if (el && el.tagName === "BUTTON" && el.id !== "pplay") return;
      e.preventDefault(); pplayEl.click(); break;
    case "ArrowLeft":  e.preventDefault(); skip(-SKIP_SECONDS); break;
    case "ArrowRight": e.preventDefault(); skip(SKIP_SECONDS); break;
    case "ArrowUp":    e.preventDefault(); prevTrack(); break;
    case "ArrowDown":  e.preventDefault(); nextTrack(); break;
    case "i": case "I": {
      if (!S.canSample || S.liveMode) break;
      e.preventDefault();
      identifyAt(keptTrackAt(audio.currentTime || 0), audio.currentTime || 0);
      break;
    }
    case "s": case "S": {
      const t = keptTrackAt(audio.currentTime || 0);
      if (t && splitAt(t, audio.currentTime || 0)) e.preventDefault();
      break;
    }
  }
});
