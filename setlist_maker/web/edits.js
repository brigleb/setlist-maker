// Edits shared by the list, timeline, inspector and issue queue.

import { S, TL } from "./state.js";
import { audio, toast } from "./dom.js";
import { fmt } from "./util.js";
import { clampStart, splitWindow } from "./model.js";
import { setDirty } from "./history.js";
import { sampleAt } from "./inspector.js";
import { render } from "./main.js";
import { showTab } from "./shell.js";

// ---- inspector ----
// Commits an artist/title change the same way the list's inline editor does,
// so the save payload -- and apply_track_edit()'s correction learning -- see
// no difference between the two.
export function setNames(t, artist, title) {
  artist = (artist || "").trim(); title = (title || "").trim();
  if (artist === t.artist && title === t.title) return false;
  t.artist = artist; t.title = title;
  if (!t._new) t._edited = true;
  setDirty(true);
  return true;
}

// Merge track t into a neighbour ("prev" or "next"), or bring it back (null).
// Every way of merging comes through here: the inspector, the list's ✕ and
// Needs a look. A merged row is rejected, and to_markdown() drops it; its span
// goes to the kept track before it, or with merged: "next" to the one after,
// whose start moves back to this row's (#56, TL.keptWindows). The save sends
// that start. Bringing a row back clears both flags, so Unmerge needs nothing
// stored to be exact. Returns whether anything changed; the caller renders.
export function mergeTrack(t, dir) {
  if (S.liveMode) return false;
  if (dir) {
    t.rejected = true;
    // A rejected track is absent from the sidecar, which is where the star is
    // stored, so it cannot stay the episode cover.
    t.episode_cover = false;
    if (dir === "next") t.merged = "next"; else delete t.merged;
  } else {
    // A merge into the next track that was saved and reloaded leaves this row
    // on the same second as that track. Two written tracks on one timestamp
    // would share one sidecar entry, which is joined by timestamp.
    if (S.tracks.some(o => o !== t && !o.rejected && o.timestamp === t.timestamp)) {
      toast("The next track starts here now. Move its start first, then bring this back");
      return false;
    }
    t.rejected = false;
    delete t.merged;
  }
  setDirty(true);
  return true;
}

export function toggleMerge(t, dir = "prev") {
  if (mergeTrack(t, t.rejected ? null : dir)) render();
}

// Cut track t at `seconds`: the new row starts there, blank, and is selected
// so it can be named straight away. The Split button and S cut at the
// playhead; Alt-click cuts wherever the pointer is, with no need to play.
export function splitAt(t, seconds) {
  const i = S.tracks.indexOf(t);
  const at = Math.floor(seconds);
  const w = splitWindow(t);
  if (S.liveMode || i === -1 || !w || !(at > w.start && at < w.end)) return false;
  // Before the track's own row is time merged into it from the track before
  // (#56). A new row cut there would sit between that merged row and this
  // track, take the merged span over, and the cut would show nowhere.
  if (at <= t.timestamp) {
    toast("That part was merged in from the track before. Unmerge it to cut there");
    return false;
  }
  const row = { artist: "", title: "", timestamp: at, time: fmt(at), rejected: false, _new: true };
  // In timestamp order: a merged track's span runs over the rows merged into it.
  let j = i + 1;
  while (j < S.tracks.length && S.tracks[j].timestamp <= at) j++;
  S.tracks.splice(j, 0, row);
  setDirty(true);
  S.selectedTrack = row;
  showTab("track");  // the new track's fields are where it gets named
  render();
  toast("Split at " + TL.fmt(at) + " — name the new track, or ⌘Z");
  return true;
}

export function splitAtPlayhead() {
  if (splitAt(S.selectedTrack, audio.currentTime)) {
    const input = document.querySelector("#insp input.in-artist");
    if (input) input.focus();
  }
}

export function applyFix(fix) {
  if (fix.sample) {
    // Evidence, not an edit: nothing to save. Sequential, so the pins land in
    // the order they were asked for rather than racing the server's queue.
    (async () => { for (const t of fix.sample) await sampleAt(t); })();
    return;
  }
  for (const idx of fix.reject || []) if (S.tracks[idx]) mergeTrack(S.tracks[idx], "prev");
  for (const idx of fix.mergeNext || []) if (S.tracks[idx]) mergeTrack(S.tracks[idx], "next");
  for (const r of fix.retitle || []) {
    const t = S.tracks[r.index];
    if (t) setNames(t, t.artist, r.title);
  }
  setDirty(true);
  render();
}

// Move track t's start (#55), clamped so it cannot pass a neighbour. Returns
// whether it moved. One call is one undo step; a drag previews with
// previewStart() and calls this once, on release. A track that has rows merged
// into it from before starts at the first of them (#56), so that row is the
// one that moves.
export function moveStart(t, seconds) {
  const i = TL.startIndex(S.tracks, S.tracks.indexOf(t));
  if (S.liveMode || i === -1) return false;
  const row = S.tracks[i];
  const at = clampStart(i, seconds);
  if (at === row.timestamp) return false;
  previewStart(row, at);
  setDirty(true);
  render();
  return true;
}

// Where track t starts as written: its kept window's start, which a merge
// into it from before moves back (#56). A merged row's own timestamp.
export function startOf(t) {
  if (!TL) return t.timestamp;  // the list still renders without /timeline.js
  return S.tracks[TL.startIndex(S.tracks, S.tracks.indexOf(t))]?.timestamp ?? t.timestamp;
}

// Set a start without recording it: the drag's live preview. `t` is the row
// that holds the start (TL.startIndex), not necessarily the kept track.
export function previewStart(t, seconds) {
  t.timestamp = seconds;
  t.time = fmt(seconds);
}
