// The timeline grid: blocks, seams, pins, the playhead, selection.

import { GRADS, S, TL } from "./state.js";
import { audio, el, toast } from "./dom.js";
import { trackName } from "./util.js";
import { clampStart, setDuration, splitWindow, trackIndexAt } from "./model.js";
import { playAt } from "./player.js";
import { renderList, revealSelected } from "./list.js";
import { moveStart, previewStart, splitAt } from "./edits.js";
import { renderInspector, sampleAt } from "./inspector.js";
import { renderIssues } from "./issues.js";
import { artSrc } from "./artwork.js";
import { html, keyOf, render as paint } from "./ui.js";
import { showTab } from "./shell.js";
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
    document.getElementById("tl-hint").textContent =
      "click a track to inspect · drag its left edge to move its start · double-click to play from there · alt-click to split it there · S splits at the playhead · click a pin to hear what Shazam heard"
      + (S.canSample ? " · I names the track at the playhead · click the pin lane to ask Shazam about any moment" : "");
    if (wasLive && !data.live) {
      // The run finished and this server is now its editor. Everything on
      // the page was a live guess; start over from the saved tracklist.
      setLive(false);
      toast("Identification finished — the tracklist is yours to edit");
      await load();
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

// One hour of the grid: ruler, artwork thumbs, track blocks, merge seams,
// never-sampled stretches, the run's pins, and the playhead.
function Hour({ h, W, kept, shown, gaps }) {
  const a = h * TL.HOUR, b = a + TL.HOUR;
  const at = t => ((t - a) / TL.HOUR * 100) + "%";
  const span = d => (d / TL.HOUR * 100) + "%";
  const sel = t => (t === S.selectedTrack ? " sel" : "");

  // One block per track that will be written, spanning its kept window: a
  // merge reads as one block because it is one track. Each merged row stays
  // only as a seam. Seams are not click targets -- merged blips sit seconds
  // apart, a pixel or two at this scale -- so a click lands on the kept block,
  // whose inspector lists what was merged into it, each with an Unmerge.
  const segs = [], thumbs = [], edges = [];
  for (const w of kept) {
    if (w.end <= a || w.start >= b) continue;
    const i = w.index, t = S.tracks[i];
    const x = Math.max(w.start, a), y = Math.min(w.end, b);
    segs.push(html`
      <div key=${keyOf(t)} class=${"tl-seg" + (!t.artist && !t.title ? " none" : "") + sel(t)} data-i=${i}
        style=${{ "--h": blockHue(i), left: at(x), width: span(y - x) }}
        title=${trackName(t) + " · " + TL.fmt(w.start) + "–" + TL.fmt(w.end)}>
        ${y - x > 240 && html`<span>${TL.cleanTitle(t.title) || t.artist || "Unidentified"}</span>`}
      </div>`);
    // The drag handle on the block's left edge moves this track's start (#55).
    if (!S.liveMode && w.start >= a && w.start < b) {
      edges.push(html`<div key=${"edge" + keyOf(t)} class=${"tl-edge" + sel(t)} data-i=${i}
        style=${{ left: at(w.start) }} title=${"Drag to move the start of " + trackName(t) + " · ⌥ for no snapping"} />`);
    }
    if ((t.artist || t.title) && w.start >= a && w.start < b) {
      const bg = t.coverart_url
        ? "url('" + artSrc(t.coverart_url).replace(/'/g, "%27") + "')" : GRADS[i % GRADS.length];
      thumbs.push(html`<div key=${keyOf(t)} class=${"tl-thumb" + sel(t)} data-i=${i}
        style=${{ left: at(w.start), backgroundImage: bg }} title=${trackName(t) + " · " + TL.fmt(w.start)} />`);
    }
  }
  const seams = S.tracks.map((t, i) => (!t.rejected || W[i].start < a || W[i].start >= b) ? null
    : html`<div key=${"seam" + keyOf(t)} class=${"tl-seam" + sel(t)} data-i=${i} style=${{ left: at(W[i].start) }} />`);
  const unheard = gaps.map(([x0, y0], n) => (y0 <= a || x0 >= b) ? null
    : html`<div key=${"gap" + n} class="tl-unheard" title=${"Never sampled: " + TL.fmt(x0) + "–" + TL.fmt(y0)}
        style=${{ left: at(Math.max(x0, a)), width: span(Math.min(y0, b) - Math.max(x0, a)) }} />`);

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
    return html`<div key=${n} class=${cls} data-t=${p.t} style=${{ left: at(mid) }}
      title=${TL.fmt(p.t) + " · " + p.window + "s " + p.purpose + " · "
        + (hit ? (p.artist || "?") + " — " + (p.title || "?") + (p.confidence ? " (" + p.confidence + ")" : "") : "no match")} />`;
  });
  if (S.liveMode && nextProbe && replayShown === null) {
    const mid = nextProbe.t + nextProbe.window / 2;
    if (mid >= a && mid < b) {
      pins.push(html`<div key="next" class="tl-pin next" style=${{ left: at(mid) }}
        title=${"Listening here next (" + nextProbe.purpose + ")"} />`);
    }
  }
  for (const t of pendingSamples) {
    if (t < a || t >= b) continue;
    pins.push(html`<div key=${"ask" + t} class="tl-pin pending" style=${{ left: at(t) }}
      title=${"Asking Shazam about " + TL.fmt(t) + "…"} />`);
  }

  // The playhead carries no `hidden` or `style` here on purpose: updatePlayhead()
  // moves it on every timeupdate, and a prop Preact owned would be put back.
  return html`
    <div class="tl-hour">
      <div class="tl-hl">${h + "h"}</div>
      <div class="tl-lanes" data-hour=${h}>
        <div class="tl-ruler">
          ${[10, 20, 30, 40, 50].map(m => html`<span key=${m} style=${{ left: (m / 60 * 100) + "%" }}>${":" + m}</span>`)}
        </div>
        <div class="tl-art">${thumbs}</div>
        <div class="tl-blocks">${segs}${seams}${unheard}${edges}</div>
        <div class=${"tl-pins" + (S.canSample ? " can-sample" : "")}>${pins}</div>
        <div class="tl-playhead"></div>
      </div>
    </div>`;
}

// The tracklist is the truth about which tracks exist; the run's probes are
// evidence drawn over it (see web_timeline.js). Everything here is derived
// from S.tracks on each call, which render() makes, so every mutation -- edit,
// reject, insert, save, reload -- keeps the grid current with no second path.
export function renderTimeline() {
  if (!TL) return;
  const D = setDuration();
  const W = TL.windows(S.tracks, D);
  const kept = TL.keptWindows(S.tracks, D);
  const shown = replayShown === null ? S.probes : S.probes.slice(0, replayShown);
  const gaps = TL.unheard(S.probes, S.probeDuration || 0);
  const hours = Math.max(1, Math.ceil(D / TL.HOUR));
  paint(Array.from({ length: hours }, (_, h) =>
    html`<${Hour} key=${h} h=${h} W=${W} kept=${kept} shown=${shown} gaps=${gaps} />`), tlRows);
  const identified = S.tracks.filter(t => !t.rejected && (t.artist || t.title)).length;
  document.getElementById("tl-stats").textContent =
    TL.fmt(D) + " · " + S.tracks.filter(t => !t.rejected).length + " tracks (" + identified + " identified)"
    + (S.probes.length ? " · " + S.probes.length + " samples" : " · no samples saved for this set");
  document.getElementById("tl-replay").hidden = !S.probes.length;
  updatePlayhead();
  // A replay redraws every few dozen milliseconds; the inspector and the queue
  // describe all the samples, not the replay's subset, so they sit it out.
  if (replayShown === null) {
    renderInspector();
    renderIssues();
  }
}

export function updatePlayhead() {
  const t = audio.currentTime || 0;
  const show = !audio.paused || t > 0;
  document.querySelectorAll("#tl-rows .tl-lanes").forEach(lanes => {
    const a = +lanes.dataset.hour * TL.HOUR;
    const head = lanes.querySelector(".tl-playhead");
    const inside = show && t >= a && t < a + TL.HOUR;
    head.hidden = !inside;
    if (inside) head.style.left = ((t - a) / TL.HOUR * 100) + "%";
  });
  // "Split at playhead" is only meaningful while the playhead is inside the
  // selected track, which changes as it plays.
  const split = document.querySelector("#insp .in-split");
  const ident = document.querySelector("#insp .in-identify");
  if (S.selectedTrack && (split || ident)) {
    const w = splitWindow(S.selectedTrack);
    if (split) split.disabled = !(t > w.start + 1 && t < w.end - 1);
    if (ident) ident.disabled = !S.canSample || !(t >= w.start && t < w.end);
  }
}

// Seconds under the pointer, from an event inside one hour's lanes.
export function timeAt(e) {
  const lanes = e.target.closest(".tl-lanes");
  if (!lanes) return null;
  const r = lanes.getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  return Math.min(setDuration(), +lanes.dataset.hour * TL.HOUR + f * TL.HOUR);
}

// Choosing a track shows it in the inspector's Track tab -- except from the
// "Needs a look" tab (`keepTab`), where the list and the timeline already
// show the selection and switching away would lose the user's place.
export function selectTrack(t, opts) {
  if (!t) return;
  S.selectedTrack = t;
  if (!(opts && opts.keepTab)) showTab("track");
  renderList();
  renderTimeline();
  if (replayShown !== null) renderInspector();  // renderTimeline() skips it mid-replay
  // The list sits beside the timeline now, so its row follows the selection
  // ("nearest": a row already in view stays put, so a click in the list
  // doesn't jump it).
  revealSelected();
  if (opts && opts.reveal) {
    const i = S.tracks.indexOf(t);
    const block = tlRows.querySelector('.tl-blocks [data-i="' + i + '"]');
    if (block) block.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
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
  if (hit) { selectTrack(S.tracks[+hit.dataset.i]); return; }
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
  const lanes = e.target.closest(".tl-lanes");
  let cur = tlRows.querySelector(".tl-cursor");
  if (e.target.closest(".tl-edge")) { if (cur) cur.remove(); return; }
  const onPins = S.canSample && e.target.closest(".tl-pins") && !e.target.closest(".tl-pin");
  if (!lanes || !(e.target.closest(".tl-blocks") || onPins)) { if (cur) cur.remove(); return; }
  if (!cur || cur.parentNode !== lanes) { if (cur) cur.remove(); cur = el("div", "tl-cursor"); lanes.appendChild(cur); }
  const t = timeAt(e);
  const r = lanes.getBoundingClientRect();
  cur.style.left = (e.clientX - r.left) + "px";
  cur.style.top = onPins ? "58px" : "28px";
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

// The time under the pointer: whichever hour's lanes it is over (a drag may
// cross hours), else extrapolated from the hour it started in.
function timeAtPoint(x, y, home) {
  const hit = document.elementFromPoint(x, y);
  const lanes = (hit && hit.closest(".tl-lanes")) || home;
  const r = lanes.getBoundingClientRect();
  return { t: +lanes.dataset.hour * TL.HOUR + (x - r.left) / r.width * TL.HOUR, pxPerSec: r.width / TL.HOUR, lanes };
}

function dragReadout(t, x, lanes) {
  const D = setDuration();
  const kept = TL.keptWindows(S.tracks, D);
  const k = kept.findIndex(w => S.tracks[w.index] === t);
  const len = w => (w ? TL.fmt(w.end - w.start) : "—");
  drag.tip.textContent = "Start " + TL.fmt(t.timestamp) + " · previous " + len(kept[k - 1]) + " · this " + len(kept[k])
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
  const t = S.tracks[+edge.dataset.i];
  if (!t) return;
  // Captured on the container, not the handle: Preact replaces the handle
  // when the boundary crosses into another hour, and capture would go with it.
  tlRows.setPointerCapture(e.pointerId);
  const tip = el("div", "tl-cursor tl-dragtip");
  document.body.appendChild(tip);
  drag = { t, from: t.timestamp, home: edge.closest(".tl-lanes"), snaps: TL.snapPoints(S.probes),
           tip, pointerId: e.pointerId, snapped: false, frame: 0 };
  document.body.classList.add("tl-dragging");
  document.addEventListener("keydown", onDragKey, true);
  selectTrack(t);
  dragReadout(t, e.clientX, drag.home);
});

tlRows.addEventListener("pointermove", e => {
  if (!drag || e.pointerId !== drag.pointerId) return;
  const { x, y, alt } = { x: e.clientX, y: e.clientY, alt: e.altKey };
  if (drag.frame) return;  // one preview per frame, however fast the pointer
  drag.frame = requestAnimationFrame(() => {
    if (!drag) return;
    drag.frame = 0;
    const { t: raw, pxPerSec, lanes } = timeAtPoint(x, y, drag.home);
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
    dragReadout(drag.t, x, lanes);
  });
});

tlRows.addEventListener("pointerup", e => { if (drag && e.pointerId === drag.pointerId) endDrag(true); });
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
