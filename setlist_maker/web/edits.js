// Edits shared by the list, timeline, inspector and issue queue.

import { S, TL } from "./state.js";
import { audio, toast } from "./dom.js";
import { fmt } from "./util.js";
import { reindexPlayingTrack, splitWindow } from "./model.js";
import { setDirty } from "./history.js";
import { sampleAt } from "./inspector.js";
import { render } from "./main.js";

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

export function toggleMerge(t) {
  t.rejected = !t.rejected;
  if (t.rejected) t.episode_cover = false;  // same rule as the list's ✕
  setDirty(true);
  render();
}

// Cut track t at `seconds`: the new row starts there, blank, and is selected
// so it can be named straight away. The Split button and S cut at the
// playhead; Alt-click cuts wherever the pointer is, with no need to play.
export function splitAt(t, seconds) {
  const i = S.tracks.indexOf(t);
  const at = Math.floor(seconds);
  const w = splitWindow(t);
  if (S.liveMode || i === -1 || !w || !(at > w.start && at < w.end)) return false;
  const row = { artist: "", title: "", timestamp: at, time: fmt(at), rejected: false, _new: true };
  // In timestamp order: a merged track's span runs over the rows merged into it.
  let j = i + 1;
  while (j < S.tracks.length && S.tracks[j].timestamp <= at) j++;
  S.tracks.splice(j, 0, row);
  reindexPlayingTrack();
  setDirty(true);
  S.selectedTrack = row;
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
  for (const idx of fix.reject || []) {
    const t = S.tracks[idx];
    if (!t) continue;
    t.rejected = true;
    t.episode_cover = false;
  }
  for (const r of fix.retitle || []) {
    const t = S.tracks[r.index];
    if (t) setNames(t, t.artist, r.title);
  }
  setDirty(true);
  render();
}
