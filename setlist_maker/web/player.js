// The <audio> transport: track windows, the scrubber, prev/next.

import { S, TL } from "./state.js";
import { audio, playerEl, pplayEl } from "./dom.js";
import { fmt } from "./util.js";
import { fmtWindowTimes, playingIndex, trackIndexAt, windowFor } from "./model.js";
import { renderList, scrollToPlaying } from "./list.js";
import { renderTimeline, selectTrack, updatePlayhead } from "./grid.js";
import { artFor } from "./artwork.js";

export const SKIP_SECONDS = 15;
export const PREV_RESTART_THRESHOLD = 3;  // ⏮ within this many seconds goes to the previous track

// Seeks clamp to the audio's real bounds, never to the track window: crossing
// a boundary is how a user checks whether that boundary is right.
export function seekTo(seconds) {
  if (!audio.duration) return;
  audio.currentTime = Math.max(0, Math.min(seconds, audio.duration));
  syncPlayerUI();
}

// Point the player at track i without seeking. `scroll` brings its row into
// view -- true only on automatic advance, since a row the user just clicked is
// already under their cursor and yanking the list would be hostile.
export function setPlayingIndex(i, opts) {
  if (i === null || !S.tracks[i]) return;
  S.playingTrack = S.tracks[i];
  // Follow the playhead (#58): the selection moves with the audio. Before
  // the scroll below, so the playing row's centring is the scroll that wins.
  // Never while a field has focus: the inspector would change track under
  // the text being typed.
  if (S.follow && S.selectedTrack !== S.playingTrack && !typingInField()) {
    selectTrack(S.playingTrack, { keepTab: true });
  }
  syncPlayerBar();
  renderList();  // the .playing row
  if (opts && opts.scroll) scrollToPlaying();
}

// The bar's label, count and thumbnail for the playing track. render() calls
// this too: an edit to the playing track's name, or an insert above it,
// changes what the bar should say.
export function syncPlayerBar() {
  const i = playingIndex();
  if (i === null) return;
  const t = S.tracks[i];
  // Only a track missing *both* halves is unidentified; one that knows just
  // its title says so, in the same words the saved markdown uses (#44).
  document.getElementById("plabel").textContent =
    (t.artist || t.title ? (t.artist || "Unknown artist") : "Unidentified") +
    (t.title ? " — " + t.title : "");
  document.getElementById("pcount").textContent = "track " + (i + 1) + " of " + S.tracks.length;
  const pt = document.getElementById("pthumb");
  // backgroundImage, never the `background` shorthand -- see the CSS comment.
  pt.style.backgroundImage = artFor(t, i);
}

// `scroll` defaults to true: any jump the user did not initiate by clicking a
// specific row should bring the destination into view. Only the row's own ▶
// passes false, because that row is already under the cursor.
export function playFrom(i, opts) {
  const scroll = !opts || opts.scroll !== false;
  setPlayingIndex(i, { scroll });
  // Choosing to play a track is choosing to look at it; automatic advance
  // (setPlayingIndex from timeupdate) deliberately does not move the selection.
  selectTrack(S.tracks[i], { keepTab: true });  // playing is not inspecting: ⌥↑/⌥↓ leave the tab alone
  // Capture the track now, not inside start(): if metadata is still
  // loading, start() runs later and a re-indexed `tracks[i]` at that point
  // could be a different track (or throw if tracks was replaced by load()).
  const track = S.tracks[i];
  const start = () => { audio.currentTime = track.timestamp; audio.play(); };
  if (audio.readyState >= 1) start();
  else audio.addEventListener("loadedmetadata", start, { once: true });
}

export function skip(delta) { seekTo(audio.currentTime + delta); }

// ⇧Space: the selected track from its start, which is choosing to play it.
export function playSelected() {
  const i = S.tracks.indexOf(S.selectedTrack);
  if (i !== -1) playFrom(i, { scroll: false });
}

// J / K / L. A browser can't play audio backwards, so J slows a fast shuttle
// down and otherwise steps back. Any pause drops back to 1x (below).
export const SHUTTLE_RATES = [1, 1.5, 2];
export function shuttle(k) {
  const at = SHUTTLE_RATES.indexOf(audio.playbackRate);
  if (k === "k") audio.pause();
  else if (k === "l") {
    if (audio.paused) audio.play();
    else audio.playbackRate = SHUTTLE_RATES[Math.min(SHUTTLE_RATES.length - 1, at + 1)];
  } else if (!audio.paused && at > 0) audio.playbackRate = SHUTTLE_RATES[at - 1];
  else skip(-5);
}

// Follow the playhead, remembered per browser.
const followEl = document.getElementById("pfollow");
function readFollow() {
  try { return localStorage.getItem("player.follow") === "1"; } catch (e) { return false; }
}
export function toggleFollow(on = !S.follow) {
  S.follow = on;
  try { localStorage.setItem("player.follow", on ? "1" : "0"); } catch (e) { /* private window */ }
  followEl.setAttribute("aria-pressed", String(on));
  followEl.classList.toggle("on", on);
  if (on && S.playingTrack && S.selectedTrack !== S.playingTrack) selectTrack(S.playingTrack, { keepTab: true });
}
followEl.onclick = () => toggleFollow();
// Deferred like onMetadata below: selectTrack reads other modules' state.
queueMicrotask(() => { if (readFollow()) toggleFollow(true); });

function typingInField() {
  const f = document.activeElement;
  return !!f && (f.tagName === "TEXTAREA" || (f.tagName === "INPUT" && f.type !== "range"));
}

// Prev/next step through `tracks` by position, so rejected tracks are included
// -- a track is often auditioned before the decision to un-reject it.
export function prevTrack() {
  const i = playingIndex() ?? trackIndexAt(audio.currentTime);
  if (i === null) return;
  // Familiar transport convention: restart this track unless already near its start.
  const target = (audio.currentTime - S.tracks[i].timestamp > PREV_RESTART_THRESHOLD)
    ? i : Math.max(0, i - 1);
  playFrom(target);
}

export function nextTrack() {
  const i = playingIndex() ?? trackIndexAt(audio.currentTime);
  if (i === null) return;
  playFrom(Math.min(S.tracks.length - 1, i + 1));
}

// Declared before syncPlayerUI, which reads it: `let` has no hoisted value,
// so defining it after would leave a temporal-dead-zone trap for any future
// caller that runs earlier than these listeners.
export let seekDragging = false;
// The scrubber's waveform strip (drawSeekWave, below); declared here for the
// same reason.
const seekWave = document.getElementById("seek-wave");

// Reflect audio state into the bar. Called on timeupdate and after any seek.
export function syncPlayerUI() {
  const seek = document.getElementById("seek");
  document.getElementById("ptime").textContent =
    fmt(audio.currentTime) + " / " + fmt(audio.duration)
    + (audio.playbackRate !== 1 ? " · " + audio.playbackRate + "×" : "");
  // The strip keeps its height from the moment there is a waveform, so the
  // bar does not grow when playback first starts.
  seekWave.hidden = !S.peaks || !TL;
  const p = playingIndex();
  if (p === null) return;
  const w = windowFor(p);
  const frac = Math.max(0, Math.min((audio.currentTime - w.start) / w.length, 1));
  if (!seekDragging) seek.value = String(Math.round(frac * 1000));
  drawSeekWave(w, frac);
  const times = fmtWindowTimes(p, audio.currentTime);
  document.getElementById("ppos").textContent = times.into + " · " + times.left;
}

// The scrubber's picture of the audio (#57): the playing track's window, the
// played part in the accent colour, and a tick at each boundary. A canvas
// above the native range input rather than a restyled track, which would
// lose the input's system look.
function drawSeekWave(w, frac) {
  if (seekWave.hidden) return;
  const dpr = window.devicePixelRatio || 1;
  const cw = Math.round(seekWave.clientWidth * dpr), ch = Math.round(seekWave.clientHeight * dpr);
  if (!cw || !ch) return;
  if (seekWave.width !== cw) seekWave.width = cw;
  if (seekWave.height !== ch) seekWave.height = ch;
  const g = seekWave.getContext("2d");
  g.clearRect(0, 0, cw, ch);
  const cs = getComputedStyle(document.documentElement);
  const wave = cs.getPropertyValue("--player-muted").trim(), accent = cs.getPropertyValue("--accent").trim();
  const cols = TL.peakColumns(S.peaks.levels, S.peaks.rate, w.start, w.length, cw);
  const played = Math.round(frac * cw), mid = ch / 2, k = (ch / 2 - 1) / 128;
  for (let x = 0; x < cw; x++) {
    const lo = cols[2 * x], hi = cols[2 * x + 1];
    g.fillStyle = x < played ? accent : wave;
    g.fillRect(x, mid - hi * k, 1, Math.max(1, (hi - lo) * k));
  }
  g.fillStyle = accent;
  g.fillRect(0, 0, Math.ceil(dpr), ch);
  g.fillRect(cw - Math.ceil(dpr), 0, Math.ceil(dpr), ch);
}

pplayEl.onclick = () => { if (audio.paused) audio.play(); else audio.pause(); };
document.getElementById("pback15").onclick = () => skip(-SKIP_SECONDS);
document.getElementById("pfwd15").onclick = () => skip(SKIP_SECONDS);
document.getElementById("pprev").onclick = prevTrack;
document.getElementById("pnext").onclick = nextTrack;

audio.addEventListener("play", () => pplayEl.textContent = "⏸");
audio.addEventListener("pause", () => { pplayEl.textContent = "▶"; audio.playbackRate = 1; });
audio.addEventListener("ratechange", () => syncPlayerUI());
function onMetadata() {
  playerEl.classList.remove("disabled");
  syncPlayerUI();
  renderTimeline();  // the real duration can differ from the saved one
}
audio.addEventListener("loadedmetadata", onMetadata);
// Module scripts run after the document has parsed, by which time a quick
// <audio preload="metadata"> may already have fired the event above; the
// player would then stay disabled, and with it every playback key. Deferred:
// renderTimeline() reads state in modules that may not have run yet.
if (audio.readyState >= 1) queueMicrotask(onMetadata);  // after every module has loaded
audio.addEventListener("timeupdate", () => updatePlayhead());
audio.addEventListener("error", () => playerEl.classList.add("disabled"));
audio.addEventListener("timeupdate", syncPlayerUI);
// Playback deliberately runs past a window's end: hearing the transition is
// how a boundary gets verified. Re-scope to whatever track we are now inside.
audio.addEventListener("timeupdate", () => {
  const i = trackIndexAt(audio.currentTime);
  if (i !== null && i !== playingIndex()) {
    setPlayingIndex(i, { scroll: true });
    // The syncPlayerUI() listener above already ran this tick against the
    // old index, so the thumb/position would otherwise show one stale
    // frame (pegged to the old window's edge) until the next timeupdate.
    syncPlayerUI();
  }
});

export const seekEl = document.getElementById("seek");
seekEl.addEventListener("pointerdown", () => { seekDragging = true; });
// A drag the OS interrupts (gesture cancelled, pointer lost) ends in
// pointercancel with no pointerup; a flag stuck true would keep timeupdate
// from moving the thumb until the next complete drag cleared it.
export const endSeekDrag = () => { seekDragging = false; };
seekEl.addEventListener("pointerup", endSeekDrag);
seekEl.addEventListener("pointercancel", endSeekDrag);
seekEl.addEventListener("input", e => {
  // Same fallback as skip()/prevTrack()/nextTrack(): before the first play
  // playingIndex is null, but the scrubber should still work.
  const i = playingIndex() ?? trackIndexAt(audio.currentTime);
  if (i === null) return;
  const w = windowFor(i);
  seekTo(w.start + (e.target.value / 1000) * w.length);
});

// Start playback at an arbitrary second (a pin, a double-click) rather than
// at a track's start, and let the player's own timeupdate re-scoping find
// which track that is.
export function playAt(seconds) {
  const go = () => {
    audio.currentTime = Math.max(0, seconds);
    const i = trackIndexAt(audio.currentTime);
    if (i !== null) setPlayingIndex(i, { scroll: false });
    // A pause before playback starts (the replace dialog's Listen, closed at
    // once) rejects this promise; that is not an error worth reporting.
    audio.play().catch(() => {});
  };
  if (audio.readyState >= 1) go();
  else audio.addEventListener("loadedmetadata", go, { once: true });
}
