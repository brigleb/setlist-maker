// Entry point: load, render, save, Done, and the live (--watch) poll.

import { S, TL } from "./state.js";
import { audio, autosizeSummary, epArtist, epTitle, summaryEl, toast } from "./dom.js";
import { playingIndex, setDuration, trackIndexAt, windowFor } from "./model.js";
import { resetHistory, setDirty } from "./history.js";
import { renderList } from "./list.js";
import { syncPlayerBar } from "./player.js";
import { loadProbes, renderTimeline } from "./grid.js";
import { closeArtwork, overlay, renderCover } from "./artwork.js";
import "./util.js";
import "./edits.js";
import "./inspector.js";
import "./issues.js";
import "./keys.js";
import "./shell.js";

export let revision = null;
export async function load() {
  const data = await (await fetch("/api/tracklist")).json();
  document.getElementById("file").textContent = "🎚 " + data.source_file;
  // A reload replaces every track object, so the selection is carried over
  // by what it pointed at rather than by reference.
  const was = S.selectedTrack;
  // ...and the player by position: a reload keeps the order.
  const playing = playingIndex();
  setLive(!!data.live);
  S.tracks = data.tracks;
  S.playingTrack = playing === null ? null : S.tracks[playing] || null;
  S.editing = null;
  // Sent back with every save: the server refuses one made against an older
  // revision, whose indices would land on other tracks.
  revision = data.revision === undefined ? null : data.revision;
  if (was) S.selectedTrack = S.tracks.find(t => t.timestamp === was.timestamp && t.artist === was.artist) || null;
  summaryEl.value = data.summary || "";
  autosizeSummary();
  epTitle.value = (data.episode && data.episode.title) || "";
  epArtist.value = (data.episode && data.episode.artist) || "";
  S.coverVersion = data.cover === undefined ? null : data.cover;
  S.coverChoice = undefined;
  S.canEmbed = !!data.can_embed;
  resetHistory();
  render();
  loadProbes();
}

export function updateCounts() {
  const rej = S.tracks.filter(t => t.rejected).length;
  const ed = S.tracks.filter(t => t._edited).length;
  const parts = [S.tracks.length + " tracks"];
  if (ed) parts.push(ed + " edited");
  if (rej) parts.push(rej + " rejected");
  document.getElementById("counts").textContent = parts.join(" · ");
}

// Every edit ends here. Each pane is a Preact render, so this updates the
// rows, blocks and fields that changed rather than rebuilding the page.
export function render() {
  renderList();
  updateCounts();
  syncPlayerBar();
  renderTimeline();
  renderCover();
}

document.getElementById("save").onclick = () => save();

// Returns whether the save landed, so Embed chapters can save first. A save
// asked for while one is in flight (a double click, ⌘S then Embed) joins it
// rather than sending the same rows again.
export let saving = null;
export function save() {
  if (!saving) saving = doSave().finally(() => { saving = null; });
  return saving;
}

export async function doSave() {
  // Existing rows map back by their stable server index (NOT array position,
  // which inserted rows shift); new rows carry a timestamp and no index.
  const hasNew = S.tracks.some(t => t._new);
  const payload = { revision: revision, summary: summaryEl.value,
                    episode: { title: epTitle.value, artist: epArtist.value },
                    tracks: S.tracks.map(t => {
    if (t._new) return { artist: t.artist, title: t.title, timestamp: t.timestamp, rejected: !!t.rejected };
    const edit = { index: t.index, artist: t.artist, title: t.title, timestamp: t.timestamp,
                   rejected: !!t.rejected, episode_cover: !!t.episode_cover };
    // Only rows whose artwork the user actually picked carry a coverart_url.
    // Sending it for every row would re-pin the art that apply_track_edit()
    // deliberately drops when an artist/title is corrected (#30).
    if (t._art) edit.coverart_url = t.coverart_url;
    return edit;
  }) };
  if (S.coverChoice !== undefined) payload.cover = S.coverChoice;
  try {
    const r = await fetch("/api/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const res = await r.json();
    if (res.ok) {
      // Always re-render: an edited artist/title changes the composite, and
      // the thumb must re-request against the now-saved server state.
      S.artVersion++;
      // The picks are on the server now, so rows go back to previewing its
      // regenerated composite instead of the raw image that was chosen.
      S.tracks.forEach(t => { delete t._art; });
      // The open panel is showing a pre-save preview and a "Save to apply"
      // note, and after a reload its track object is stale; close it rather
      // than leave either lying.
      if (overlay.classList.contains("open")) closeArtwork();
      if (S.coverChoice !== undefined) { S.coverVersion = S.coverChoice ? Date.now() : null; S.coverChoice = undefined; }
      // Inserted rows need real indices, and a save that re-sorted the tracks
      // (a moved start) bumped the revision: either way, reload first.
      if (hasNew || res.revision !== revision) await load();
      else render();
      setDirty(false); resetHistory(); toast("Saved");
      return true;
    }
    toast("Save failed: " + (res.error || ""));
  } catch (e) { toast("Save failed"); }
  return false;
}

document.getElementById("done").onclick = async () => {
  if (S.dirty && !confirm("You have unsaved changes. Close anyway?")) return;
  S.dirty = false;
  await fetch("/api/done", { method: "POST" }).catch(() => {});
  document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;color:var(--muted);font-family:sans-serif">Editor closed — you can close this tab.</div>';
  setTimeout(() => { try { window.close(); } catch (e) {} }, 300);
};

window.addEventListener("beforeunload", e => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });

// One poll at a time: a replay slower than the interval must not stack, or a
// late answer could land after the switch to editing.
export let polling = false;
export async function pollLive() {
  if (polling || !S.liveMode) return;
  polling = true;
  try { await loadProbes(); } finally { polling = false; }
}

export function setLive(on) {
  S.liveMode = on;
  document.body.classList.toggle("live", on);
  summaryEl.readOnly = on;
  epTitle.readOnly = epArtist.readOnly = on;
  if (on && !S.livePoll) S.livePoll = setInterval(pollLive, 3000);
  if (!on && S.livePoll) { clearInterval(S.livePoll); S.livePoll = null; }
}

// A live run's tracklist is re-derived after every probe, so boundaries move
// and tracks appear. Keep the selection on whatever track now covers the
// moment that was selected, and the player on whatever covers the playhead.
export async function refreshLiveTracks() {
  let focus = null;
  if (S.selectedTrack) {
    const i = S.tracks.indexOf(S.selectedTrack);
    const w = i === -1 ? null : windowFor(i);
    if (w) focus = (w.start + w.end) / 2;
  }
  const data = await (await fetch("/api/tracklist")).json();
  if (!S.liveMode || !data.live) return;  // the run finished meanwhile; load() owns the page now
  S.tracks = data.tracks;
  resetHistory();
  if (focus !== null) {
    const W = TL.windows(S.tracks, setDuration());
    const k = W.findIndex(w => focus >= w.start && focus < w.end);
    S.selectedTrack = k === -1 ? null : S.tracks[k];
  }
  if (S.playingTrack) {
    S.playingTrack = null;
    const k = trackIndexAt(audio.currentTime);
    if (k !== null && !audio.paused) S.playingTrack = S.tracks[k];
  }
  render();
}

load();
