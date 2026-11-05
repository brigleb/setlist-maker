// The timeline grid: blocks, seams, pins, the playhead, selection.

import { begin } from "./activity.js";
import { S, TL } from "./state.js";
import { audio, el, toast } from "./dom.js";
import { trackName } from "./util.js";
import { clampStart, setDuration, splitWindow, trackIndexAt } from "./model.js";
import { playAt, syncPlayerUI } from "./player.js";
import { renderList, revealSelected } from "./list.js";
import { moveStart, previewStart, splitAt } from "./edits.js";
import { renderInspector, sampleAt } from "./inspector.js";
import { renderIssues } from "./issues.js";
import { syncRunControls } from "./app.js";
import { artFor } from "./artwork.js";
import { html, keyOf, render as paint } from "./ui.js";
import { showTab } from "./shell.js";
import { clickMods, isSelected, pointSelection } from "./selection.js";
import { load, refreshLiveTracks, render, setLive } from "./main.js";

export let replayShown = null;  // during a replay, how many probes are visible
export let nextProbe = null;    // where the live run will listen next
export let seenProbes = 0;      // pins already drawn, so only new ones drop in
export const pendingSamples = [];  // seconds asked about and not yet answered

export async function loadProbes() {
  try {
    const data = await (await fetch("/api/probes")).json();
    const wasLive = S.liveMode;
    seenProbes = S.probes.length;
    S.probes = data.probes || [];
    S.probeDuration = data.duration || null;
    S.canSample = !!data.can_sample;
    nextProbe = data.next || null;
    if (S.paused !== (data.paused || null)) { S.paused = data.paused || null; syncRunControls(); }
    if (wasLive && !data.live) {
      // The run finished and this server is now its editor. Everything on
      // the page was a live guess; start over from the saved tracklist. (In
      // the app a stopped run leaves nothing open: the drop screen.)
      setLive(false);
      if (await load()) toast("Identification finished — the tracklist is yours to edit");
      return;
    }
    if (data.live && S.liveMode && S.probes.length !== seenProbes) await refreshLiveTracks();
  } catch (e) {
    S.probes = [];  // no evidence to draw; the tracklist alone still makes a timeline
  }
  renderList();  // its Samples column counts them
  renderTimeline();
}

export function blockHue(i) {
  return TL.hue(TL.identity(S.tracks[i].artist, S.tracks[i].title) || "?");
}

// ---- the view (#57) ----
// One zoomable, pannable row over the whole set. `view` is the stretch on
// screen, { start, span } in seconds; every position on the page -- blocks,
// pins, the playhead, the cursor, a click, a drag -- goes through xOf()/tAt()
// against it. ⌘-scroll or a pinch zooms around the pointer, scrolling pans, Z
// fits the selected track and 0 the whole set. The overview strip above shows
// the whole set with the view as a box that can be dragged.
const viewEl = document.getElementById("tl-view");
const rulerEl = document.getElementById("tl-ruler");
const bandEl = document.getElementById("tl-band");
const blocksEl = document.getElementById("tl-blocks");
const pinsEl = document.getElementById("tl-pins");
const waveEl = document.getElementById("tl-wave");
const miniEl = document.getElementById("tl-mini");
const miniWave = document.getElementById("tl-mini-wave");
const miniBox = document.getElementById("tl-mini-box");
const headEl = viewEl.querySelector(".tl-playhead");

export let view = { start: 0, span: 0 };
let fitAll = true;  // follow the set's length (the audio's duration arrives late)
let paneW = 1, bandH = 0;

const fitView = v => TL.clampView(v, setDuration(), paneW);
function setView(v) {
  view = fitView(v);
  fitAll = view.span >= setDuration() - 0.5;
  requestPaint();
}
export function zoomAll() { setView({ start: 0, span: setDuration() }); }
export function zoomBy(factor, frac = 0.5) { setView(TL.zoomAt(view, factor, frac, setDuration(), paneW)); }
// Z: the selected track's kept window with a little either side.
export function zoomToTrack(t) {
  const i = S.tracks.indexOf(t);
  if (i === -1) return false;
  const D = setDuration();
  const w = TL.keptWindows(S.tracks, D).find(x => x.index === i) || TL.windows(S.tracks, D)[i];
  const pad = Math.max(5, (w.end - w.start) * 0.08);
  setView({ start: w.start - pad, span: w.end - w.start + 2 * pad });
  return true;
}
// Bring a track into view if none of it is showing, keeping the zoom.
function revealInView(t) {
  const i = S.tracks.indexOf(t);
  if (i === -1) return;
  const D = setDuration();
  const w = TL.keptWindows(S.tracks, D).find(x => x.index === i) || TL.windows(S.tracks, D)[i];
  if (w.end > view.start && w.start < view.start + view.span) return;
  setView({ start: w.start - view.span * 0.1, span: view.span });
}

const xPct = t => ((t - view.start) / view.span * 100) + "%";
const wPct = d => (d / view.span * 100) + "%";
// Seconds at a client x, from the view pane.
function tAtX(clientX) {
  const r = viewEl.getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
  return Math.min(setDuration(), view.start + f * view.span);
}

// Colours come from the theme's tokens, so the canvases follow dark mode.
let palette = null;
const colours = () => palette || (palette = (() => {
  const cs = getComputedStyle(document.documentElement);
  return { wave: cs.getPropertyValue("--wave").trim() || "#888", blockL: cs.getPropertyValue("--block-l").trim() || "60%" };
})());
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { palette = null; paintMini(); requestPaint(); });

// A canvas sized to its box in device pixels, cleared; null while hidden.
function prep(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(canvas.clientWidth * dpr), h = Math.round(canvas.clientHeight * dpr);
  if (!w || !h) return null;
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  const g = canvas.getContext("2d");
  g.clearRect(0, 0, w, h);
  return { g, w, h };
}

function drawWave(c, start, span, top, height) {
  if (!S.peaks || !c) return;
  const cols = TL.peakColumns(S.peaks.levels, S.peaks.rate, start, span, c.w);
  const mid = top + height / 2, k = height / 2 / 128;
  c.g.fillStyle = colours().wave;
  for (let x = 0; x < c.w; x++) {
    const lo = cols[2 * x], hi = cols[2 * x + 1];
    if (hi === 0 && lo === 0) continue;
    c.g.fillRect(x, mid - hi * k, 1, Math.max(1, (hi - lo) * k));
  }
}

// The overview: every kept track's colour along the top, the whole set's
// waveform under it. Redrawn when the tracks or the theme change; the view
// box moves on its own in paintView().
export function paintMini() {
  if (!TL) return;
  const c = prep(miniWave);
  if (!c) return;
  const D = setDuration();
  const stripe = Math.round(5 * (window.devicePixelRatio || 1));
  for (const w of TL.keptWindows(S.tracks, D)) {
    const t = S.tracks[w.index];
    c.g.fillStyle = !t.artist && !t.title ? colours().wave : "hsl(" + blockHue(w.index) + " 45% " + colours().blockL + ")";
    c.g.fillRect(Math.floor(w.start / D * c.w), 0, Math.max(1, Math.ceil((w.end - w.start) / D * c.w)), stripe);
  }
  drawWave(c, 0, D, stripe + 1, c.h - stripe - 1);
}

let paintQueued = 0;
export function requestPaint() {
  if (paintQueued) return;
  paintQueued = requestAnimationFrame(() => { paintQueued = 0; paintView(); });
}

// Everything that moves with the view: blocks, pins, ruler, waveform, the
// overview box, the playhead. Cheap enough for every frame of a pan; the
// inspector and the queue are left to renderTimeline(), which a model change
// calls.
export function paintView() {
  if (!TL) return;
  if (paintQueued) { cancelAnimationFrame(paintQueued); paintQueued = 0; }
  const D = setDuration();
  if (fitAll || !view.span) view = fitView({ start: 0, span: D }); else view = fitView(view);
  const a = view.start, b = a + view.span;
  const px = d => d / view.span * paneW;
  const W = TL.windows(S.tracks, D);
  const kept = TL.keptWindows(S.tracks, D);
  const shown = replayShown === null ? S.probes : S.probes.slice(0, replayShown);
  const gaps = TL.unheard(S.probes, S.probeDuration || 0);
  const sel = t => (isSelected(t) ? " sel" : "");

  // One block per track that will be written, spanning its kept window: a
  // merge reads as one block because it is one track. Each merged row stays
  // only as a seam. Seams are not click targets -- merged blips sit seconds
  // apart -- so a click lands on the kept block, whose inspector lists what
  // was merged into it, each with an Unmerge. A block wide enough carries its
  // name, and wider still its artwork.
  const segs = [], edges = [];
  const thumbSide = Math.min(bandH - 6, 56);
  for (const w of kept) {
    if (w.end <= a || w.start >= b) continue;
    const i = w.index, t = S.tracks[i];
    const x = Math.max(w.start, a), y = Math.min(w.end, b);
    const wide = px(y - x);
    const named = !!(t.artist || t.title);
    const art = named && thumbSide >= 24 && wide >= thumbSide + 40 && w.start >= a;
    const bg = art ? artFor(t, i) : null;
    segs.push(html`
      <div key=${keyOf(t)} class=${"tl-seg" + (named ? "" : " none") + sel(t)} data-i=${i}
        style=${{ "--h": blockHue(i), left: xPct(x), width: wPct(y - x) }}
        title=${trackName(t) + " · " + TL.fmt(w.start) + "–" + TL.fmt(w.end)}>
        ${art && html`<i class="tl-sthumb" style=${{ backgroundImage: bg, width: thumbSide + "px", height: thumbSide + "px" }} />`}
        ${wide > 36 && html`<span style=${art ? { left: (thumbSide + 9) + "px" } : null}>${TL.cleanTitle(t.title) || t.artist || "Unidentified"}</span>`}
      </div>`);
    // The drag handle on the block's left edge moves this track's start (#55).
    if (!S.liveMode && w.start >= a && w.start < b) {
      edges.push(html`<div key=${"edge" + keyOf(t)} class=${"tl-edge" + sel(t)} data-i=${i}
        style=${{ left: xPct(w.start) }} title=${"Drag to move the start of " + trackName(t) + " · ⌥ for no snapping"} />`);
    }
  }
  // A row merged into the next track starts that track's block (#56): its
  // seam would sit on the block's own edge, so it has none.
  const starts = new Set(kept.map(w => w.start));
  const seams = S.tracks.map((t, i) => (!t.rejected || starts.has(W[i].start) || W[i].start < a || W[i].start >= b) ? null
    : html`<div key=${"seam" + keyOf(t)} class=${"tl-seam" + sel(t)} data-i=${i} style=${{ left: xPct(W[i].start) }} />`);
  const unheard = gaps.map(([x0, y0], n) => (y0 <= a || x0 >= b) ? null
    : html`<div key=${"gap" + n} class="tl-unheard" title=${"Never sampled: " + TL.fmt(x0) + "–" + TL.fmt(y0)}
        style=${{ left: xPct(Math.max(x0, a)), width: wPct(Math.min(y0, b) - Math.max(x0, a)) }} />`);

  const pins = shown.map((p, n) => {
    const mid = p.t + p.window / 2;
    if (mid < a || mid >= b) return null;
    const hit = p.artist || p.title;
    // `drop` animates a pin in: the newest one during a replay, and the ones a
    // live poll just brought.
    const drop = (replayShown !== null && n === replayShown - 1)
      || (S.liveMode && replayShown === null && n >= seenProbes && seenProbes);
    const cls = "tl-pin" + (p.purpose === "refine" ? " refine" : "") + (p.purpose === "manual" ? " manual" : "")
      + (hit ? "" : " miss") + (drop ? " drop" : "");
    return html`<div key=${n} class=${cls} data-t=${p.t} style=${{ left: xPct(mid) }}
      title=${TL.fmt(p.t) + " · " + p.window + "s " + p.purpose + " · "
        + (hit ? (p.artist || "?") + " — " + (p.title || "?") + (p.confidence ? " (" + p.confidence + ")" : "") : "no match")} />`;
  });
  if (S.liveMode && nextProbe && replayShown === null) {
    const mid = nextProbe.t + nextProbe.window / 2;
    if (mid >= a && mid < b) {
      pins.push(html`<div key="next" class="tl-pin next" style=${{ left: xPct(mid) }}
        title=${"Listening here next (" + nextProbe.purpose + ")"} />`);
    }
  }
  for (const t of pendingSamples) {
    if (t < a || t >= b) continue;
    pins.push(html`<div key=${"ask" + t} class="tl-pin pending" style=${{ left: xPct(t) }}
      title=${"Asking Shazam about " + TL.fmt(t) + "…"} />`);
  }

  const step = TL.tickStep(view.span, paneW, 70);
  const ticks = [];
  for (let t = Math.ceil(a / step) * step; t < b; t += step) {
    ticks.push(html`<span key=${t} style=${{ left: xPct(t) }}>${TL.fmt(t)}</span>`);
  }

  paint(ticks, rulerEl);
  paint([...segs, ...seams, ...unheard, ...edges], blocksEl);
  paint(pins, pinsEl);
  pinsEl.classList.toggle("can-sample", !!S.canSample);
  const c = prep(waveEl);
  if (c) drawWave(c, a, view.span, 2, c.h - 4);
  miniBox.style.left = (a / D * 100) + "%";
  miniBox.style.width = (view.span / D * 100) + "%";
  miniBox.hidden = fitAll;
  updatePlayhead();
}

new ResizeObserver(() => {
  paneW = viewEl.clientWidth || 1;
  bandH = bandEl.clientHeight;
  paintMini();
  paintView();
}).observe(viewEl);

// The waveform, fetched once a page (peaks.py). The page draws without it
// meanwhile, and a set with no audio simply has none.
let peaksAsked = false;
export async function loadPeaks() {
  if (peaksAsked) return;
  peaksAsked = true;
  const job = begin("peaks", "Reading the waveform…");
  try {
    const r = await fetch("/api/peaks");
    if (!r.ok) throw new Error(r.status);
    const buf = await r.arrayBuffer();
    const head = new DataView(buf, 0, 24);
    const magic = String.fromCharCode(...new Uint8Array(buf, 0, 4));
    if (magic !== "SMPK") throw new Error("not peaks");
    const rate = head.getUint32(8, true), count = head.getUint32(12, true);
    S.peaks = { rate, levels: TL.peakLevels(new Int8Array(buf, 24, Math.min(count * 2, buf.byteLength - 24))) };
  } catch (e) {
    S.peaks = null;
  }
  job.end();
  paintMini();
  requestPaint();
  syncPlayerUI();
}

function renderStats() {
  const D = setDuration();
  const identified = S.tracks.filter(t => !t.rejected && (t.artist || t.title)).length;
  document.getElementById("tl-stats").textContent =
    TL.fmt(D) + " · " + S.tracks.filter(t => !t.rejected).length + " tracks (" + identified + " identified)"
    + (S.probes.length ? " · " + S.probes.length + " samples" : " · no samples saved for this set");
}

// The tracklist is the truth about which tracks exist; the run's probes are
// evidence drawn over it (see web_timeline.js). Everything here is derived
// from S.tracks on each call, which render() makes, so every mutation -- edit,
// reject, insert, save, reload -- keeps the grid current with no second path.
export function renderTimeline() {
  if (!TL) return;
  paintView();
  paintMini();
  renderStats();
  document.getElementById("tl-replay").hidden = !S.probes.length;
  // A replay redraws every few dozen milliseconds; the inspector and the queue
  // describe all the samples, not the replay's subset, so they sit it out.
  if (replayShown === null) {
    renderInspector();
    renderIssues();
  }
}

// The playhead, and the view following it: when playback runs off the right
// edge of a view it was inside, the view pages on. A view somewhere else is
// left alone, so looking at one part of the set while another plays is fine.
let headWasIn = false;
export function updatePlayhead() {
  const t = audio.currentTime || 0;
  const show = !audio.paused || t > 0;
  let inside = show && t >= view.start && t < view.start + view.span;
  if (show && !inside && headWasIn && !audio.paused && !drag && t >= view.start + view.span && !fitAll) {
    view = fitView({ start: t - view.span * 0.05, span: view.span });
    requestPaint();
    inside = true;
  }
  headWasIn = inside;
  headEl.hidden = !inside;
  if (inside) headEl.style.left = xPct(t);
  // "Split at playhead" is only meaningful while the playhead is inside the
  // selected track, which changes as it plays.
  const split = document.querySelector("#insp .in-split");
  const ident = document.querySelector("#insp .in-identify");
  if (S.selectedTrack && (split || ident)) {
    const w = splitWindow(S.selectedTrack);
    if (split) split.disabled = !(t > Math.max(w.start, S.selectedTrack.timestamp) + 1 && t < w.end - 1);
    if (ident) ident.disabled = !S.canSample || !(t >= w.start && t < w.end);
    // The line under them that says why both are off.
    const hint = document.querySelector("#insp .in-hint");
    if (hint && split) hint.hidden = !(split.disabled && (!ident || ident.disabled));
  }
}

// Seconds under the pointer, from an event inside the view.
export function timeAt(e) {
  if (!e.target.closest("#tl-view")) return null;
  return tAtX(e.clientX);
}

// ---- wheel: ⌘-scroll or pinch zooms around the pointer, scrolling pans ----
document.getElementById("tl-rows").addEventListener("wheel", e => {
  e.preventDefault();
  const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? paneW : 1;
  const dx = e.deltaX * unit, dy = e.deltaY * unit;
  const r = viewEl.getBoundingClientRect();
  if (e.ctrlKey || e.metaKey) {
    // A pinch arrives as ctrl+wheel in small steps; a mouse wheel's notch is
    // ~100, capped so one notch is about 1.6x.
    const d = Math.max(-50, Math.min(50, dy));
    zoomBy(Math.exp(d * 0.01), Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)));
  } else {
    const d = Math.abs(dx) > Math.abs(dy) ? dx : dy;
    if (d) setView({ start: view.start + d * view.span / paneW, span: view.span });
  }
}, { passive: false });

// ---- the overview strip: drag the box, or press anywhere to jump there ----
miniEl.addEventListener("pointerdown", e => {
  if (e.button !== 0) return;
  e.preventDefault();
  const D = setDuration();
  const r = miniEl.getBoundingClientRect();
  const at = ev => Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)) * D;
  const t0 = at(e);
  const inBox = !fitAll && t0 >= view.start && t0 <= view.start + view.span;
  // Grabbed where it was pressed inside the box; anywhere else centres it.
  const grab = inBox ? t0 - view.start : view.span / 2;
  if (!inBox) setView({ start: t0 - grab, span: view.span });
  miniEl.setPointerCapture(e.pointerId);
  miniEl.classList.add("dragging");
  const move = ev => setView({ start: at(ev) - grab, span: view.span });
  const up = () => {
    miniEl.classList.remove("dragging");
    miniEl.removeEventListener("pointermove", move);
    miniEl.removeEventListener("pointerup", up);
    miniEl.removeEventListener("pointercancel", up);
  };
  miniEl.addEventListener("pointermove", move);
  miniEl.addEventListener("pointerup", up);
  miniEl.addEventListener("pointercancel", up);
});
miniEl.addEventListener("dblclick", zoomAll);

// Choosing a track shows it in the inspector's Track tab -- except from the
// "Needs a look" tab (`keepTab`), where the list and the timeline already
// show the selection and switching away would lose the user's place.
//
// opts.extend, opts.toggle and opts.keepMulti shape a multiple selection
// (#77, selection.pointSelection()); a plain call selects t alone, so Follow,
// the palette and Needs a look all drop one without knowing about it.
export function selectTrack(t, opts) {
  if (!t) return;
  S.selectedTrack = pointSelection(t, opts || {});
  if (!(opts && opts.keepTab)) showTab("track");
  renderList();
  renderTimeline();
  if (replayShown !== null) renderInspector();  // renderTimeline() skips it mid-replay
  // The list sits beside the timeline now, so its row follows the selection
  // ("nearest": a row already in view stays put, so a click in the list
  // doesn't jump it).
  revealSelected();
  // The timeline pans to it when none of it is in view, at the same zoom.
  revealInView(t);
}

export const tlRows = document.getElementById("tl-rows");
tlRows.addEventListener("click", e => {
  // A drag handle's click is the end of a drag (or a press that went nowhere),
  // and with ⌥ held for no snapping it would otherwise read as alt-click split.
  if (e.target.closest(".tl-edge")) return;
  const pin = e.target.closest(".tl-pin");
  if (pin) {
    const t = +pin.dataset.t;
    const i = trackIndexAt(t + 1);
    if (i !== null) selectTrack(S.tracks[i]);
    playAt(t);
    return;
  }
  const hit = e.target.closest("[data-i]");
  if (hit && e.altKey && hit.classList.contains("tl-seg")) {
    const t = timeAt(e);
    if (t !== null && !splitAt(S.tracks[+hit.dataset.i], t)) toast("Too close to the edge of the track to split there");
    return;
  }
  if (hit) { selectTrack(S.tracks[+hit.dataset.i], clickMods(e)); return; }
  if (S.canSample && e.target.closest(".tl-pins") && !e.target.closest(".tl-pin")) {
    const t = timeAt(e);
    if (t !== null) sampleAt(Math.round(t));
  }
});
tlRows.addEventListener("dblclick", e => {
  if (!e.target.closest(".tl-blocks") || e.target.closest(".tl-edge")) return;
  const t = timeAt(e);
  if (t !== null) playAt(t);
});
tlRows.addEventListener("mousemove", e => {
  if (drag) return;  // the drag draws its own readout
  const lanes = e.target.closest("#tl-view");
  let cur = viewEl.querySelector(".tl-cursor");
  if (e.target.closest(".tl-edge")) { if (cur) cur.remove(); return; }
  const onPins = S.canSample && e.target.closest(".tl-pins") && !e.target.closest(".tl-pin");
  if (!lanes || !(e.target.closest(".tl-blocks") || onPins)) { if (cur) cur.remove(); return; }
  if (!cur || cur.parentNode !== lanes) { if (cur) cur.remove(); cur = el("div", "tl-cursor"); lanes.appendChild(cur); }
  const t = timeAt(e);
  const r = lanes.getBoundingClientRect();
  cur.style.left = (e.clientX - r.left) + "px";
  cur.style.top = ((onPins ? pinsEl : bandEl).offsetTop - 18) + "px";
  const splitting = e.altKey && !onPins && !S.liveMode;
  cur.textContent = (onPins ? "Ask Shazam about " : splitting ? "Split here at " : "") + TL.fmt(t);
  cur.classList.toggle("split", splitting);
});
tlRows.addEventListener("mouseleave", () => { const cur = tlRows.querySelector(".tl-cursor"); if (cur) cur.remove(); });

// ---- dragging a boundary (#55) ----
// Pressing a block's left-edge handle and dragging moves that track's start.
// The move is previewed on the model (grid, list and inspector all follow)
// without being recorded, and committed once on release through moveStart(),
// so one drag is one undo step. Escape puts it back. The boundary snaps to
// where Shazam's answer changes (TL.snapPoints) within SNAP_PX; ⌥ turns that
// off. Release plays from AUDITION_LEAD seconds before the new cut, so the
// cut is heard straight away. At four hours a pixel is ~10s, which is why
// the snapping and the readout matter as much as the drag.
const SNAP_PX = 6;
const AUDITION_LEAD = 5;
export let drag = null;

// The time under the pointer, from the one view. A start beyond the view is
// reached by zooming out first.
function timeAtPoint(x) {
  return { t: tAtX(x), pxPerSec: paneW / view.span, lanes: viewEl };
}

function dragReadout(t, x, lanes) {
  const D = setDuration();
  const kept = TL.keptWindows(S.tracks, D);
  const k = kept.findIndex(w => S.tracks[w.index] === t);
  const len = w => (w ? TL.fmt(w.end - w.start) : "—");
  drag.tip.textContent = "Start " + TL.fmt(kept[k] ? kept[k].start : t.timestamp) + " · previous " + len(kept[k - 1]) + " · this " + len(kept[k])
    + (drag.snapped ? " · snapped" : "");
  const r = lanes.getBoundingClientRect();
  drag.tip.style.left = x + "px";
  drag.tip.style.top = (r.top + 6) + "px";
}

function endDrag(commit) {
  if (!drag) return;
  const { t, from, tip, pointerId } = drag;
  const to = t.timestamp;
  drag = null;
  tip.remove();
  document.body.classList.remove("tl-dragging");
  document.removeEventListener("keydown", onDragKey, true);
  try { tlRows.releasePointerCapture(pointerId); } catch (e) { /* already released */ }
  previewStart(t, from);  // back to where it was, so the commit is one recorded change
  if (commit && to !== from && moveStart(t, to)) {
    playAt(Math.max(0, to - AUDITION_LEAD));
  } else {
    render();
  }
}

function onDragKey(e) {
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endDrag(false); }
}

tlRows.addEventListener("pointerdown", e => {
  const edge = e.target.closest(".tl-edge");
  if (!edge || e.button !== 0 || S.liveMode) return;
  e.preventDefault();
  const owner = S.tracks[+edge.dataset.i];
  if (!owner) return;
  // The row whose timestamp is the block's start: the track's own, or the
  // first row merged into it from before (#56).
  const t = S.tracks[TL.startIndex(S.tracks, +edge.dataset.i)];
  // Captured on the container, not the handle: Preact drops the handle when
  // the boundary leaves the view, and capture would go with it.
  tlRows.setPointerCapture(e.pointerId);
  const tip = el("div", "tl-cursor tl-dragtip");
  document.body.appendChild(tip);
  drag = { t, owner, from: t.timestamp, home: viewEl, snaps: TL.snapPoints(S.probes),
           tip, pointerId: e.pointerId, snapped: false, frame: 0, x: e.clientX, alt: e.altKey };
  document.body.classList.add("tl-dragging");
  document.addEventListener("keydown", onDragKey, true);
  selectTrack(owner);
  dragReadout(owner, e.clientX, drag.home);
});

// The latest pointer position is kept on `drag` and read when the frame runs:
// a frame that closed over its first move's x would preview, and on release
// commit, a position the pointer had already left (spike 0: WebKit's 60Hz
// frames showed it, Chromium has it too).
function applyDrag() {
  if (!drag) return;
  drag.frame = 0;
  const { x, alt } = drag;
  const { t: raw, pxPerSec, lanes } = timeAtPoint(x);
  let at = raw;
  drag.snapped = false;
  if (!alt) {
    let best = null;
    for (const s of drag.snaps) if (best === null || Math.abs(s - raw) < Math.abs(best - raw)) best = s;
    if (best !== null && Math.abs(best - raw) * pxPerSec <= SNAP_PX) { at = best; drag.snapped = true; }
  }
  const i = S.tracks.indexOf(drag.t);
  previewStart(drag.t, clampStart(i, at));
  renderList();
  renderTimeline();
  dragReadout(drag.owner, x, lanes);
}

tlRows.addEventListener("pointermove", e => {
  if (!drag || e.pointerId !== drag.pointerId) return;
  drag.x = e.clientX;
  drag.alt = e.altKey;
  if (!drag.frame) drag.frame = requestAnimationFrame(applyDrag);  // one preview per frame
});

tlRows.addEventListener("pointerup", e => {
  if (!drag || e.pointerId !== drag.pointerId) return;
  // A frame still pending holds the last moves: apply them before committing.
  if (drag.frame) { cancelAnimationFrame(drag.frame); applyDrag(); }
  endDrag(true);
});
tlRows.addEventListener("pointercancel", e => { if (drag && e.pointerId === drag.pointerId) endDrag(false); });

// Drop the pins back in, in the order the run asked for them -- the coarse
// sweep first, then the refinement crowding into each boundary.
document.getElementById("tl-replay").onclick = () => {
  if (replayShown !== null || !S.probes.length) return;
  replayShown = 0;
  const step = Math.max(15, Math.min(120, 12000 / S.probes.length));
  const tick = () => {
    replayShown++;
    if (replayShown >= S.probes.length) { replayShown = null; renderTimeline(); return; }
    renderTimeline();
    setTimeout(tick, step);
  };
  tick();
};
