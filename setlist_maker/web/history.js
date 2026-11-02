// Dirty state and undo/redo.

import { S } from "./state.js";
import { autosizeSummary, epArtist, epTitle, summaryEl, toast } from "./dom.js";
import { reindexPlayingTrack } from "./model.js";
import { closeArtwork, overlay } from "./artwork.js";
import { render } from "./main.js";

summaryEl.addEventListener("input", () => { setDirty(true); autosizeSummary(); });

export function setDirty(v) {
  if (v && S.liveMode) return;  // nothing on a live page may be saved over by the run
  S.dirty = v; document.getElementById("save").disabled = !v;
  if (v) recordChange();
}

// ---- undo ----
// Every edit already ends in setDirty(true), so that is where history is
// recorded -- a new edit path cannot forget it. A snapshot is the list's order
// plus a copy of each track's fields, restored onto the *same* objects, so the
// references held for the selection, the playing track and the artwork panel
// stay valid. One user action is one step: the snapshot is taken in a
// microtask, after everything the action changes synchronously (a Tidy that
// retitles eight tracks, a commit that renames and re-sorts). A step that
// changed no track -- typing in the description, which has its own native
// undo -- is dropped. Saving clears the history: an inserted row gets its real
// index from the server then, and undoing past that would re-send it as new.
export let steps = [], stepAt = 0, savedAt = 0, recordQueued = false, savedText = "";
// The description and the episode title/artist are text fields with their
// own native undo, so they stay out of the steps and are compared instead.
export const textState = () => JSON.stringify([summaryEl.value, epTitle.value, epArtist.value]);

export function snapshot() {
  return { order: S.tracks.slice(), fields: S.tracks.map(t => Object.assign({}, t)), cover: S.coverChoice };
}

export function sameSnapshot(a, b) {
  return a.cover === b.cover && a.order.length === b.order.length && a.order.every((t, i) => t === b.order[i])
    && JSON.stringify(a.fields) === JSON.stringify(b.fields);
}

export function resetHistory() {
  steps = [snapshot()];
  stepAt = savedAt = 0;
  savedText = textState();
  syncHistoryButtons();
}

export function recordChange() {
  if (recordQueued) return;
  recordQueued = true;
  queueMicrotask(() => {
    recordQueued = false;
    const snap = snapshot();
    if (steps.length && sameSnapshot(snap, steps[stepAt])) return;
    steps.length = stepAt + 1;  // a new edit forgets what was undone
    steps.push(snap);
    if (steps.length > 200) { steps.shift(); savedAt--; }  // -1: the saved state is gone
    stepAt = steps.length - 1;
    syncHistoryButtons();
  });
}

export function restore(to) {
  if (S.liveMode || to < 0 || to >= steps.length) return;
  stepAt = to;
  const snap = steps[to];
  S.tracks = snap.order.slice();
  S.tracks.forEach((t, i) => {
    for (const k of Object.keys(t)) if (!(k in snap.fields[i])) delete t[k];
    Object.assign(t, snap.fields[i]);
  });
  S.coverChoice = snap.cover;
  reindexPlayingTrack();
  if (S.selectedTrack && !S.tracks.includes(S.selectedTrack)) S.selectedTrack = null;
  if (overlay.classList.contains("open")) closeArtwork();  // it may show a pick being undone
  S.dirty = stepAt !== savedAt || textState() !== savedText;
  document.getElementById("save").disabled = !S.dirty;
  syncHistoryButtons();
  render();
}

export function syncHistoryButtons() {
  document.getElementById("undo").disabled = stepAt === 0;
  document.getElementById("redo").disabled = stepAt >= steps.length - 1;
}

export const undo = () => { if (stepAt > 0) { restore(stepAt - 1); toast("Undone"); } };
export const redo = () => { if (stepAt < steps.length - 1) { restore(stepAt + 1); toast("Redone"); } };
document.getElementById("undo").onclick = undo;
document.getElementById("redo").onclick = redo;
