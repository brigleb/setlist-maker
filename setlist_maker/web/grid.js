// The timeline grid: blocks, seams, pins, the playhead, selection.

import { GRADS, S, TL } from "./state.js";
import { audio, el, toast } from "./dom.js";
import { trackName } from "./util.js";
import { setDuration, splitWindow, trackIndexAt } from "./model.js";
import { playAt } from "./player.js";
import { splitAt } from "./edits.js";
import { renderInspector, sampleAt } from "./inspector.js";
import { renderIssues } from "./issues.js";
import { artSrc } from "./artwork.js";
import { load, refreshLiveTracks, setLive } from "./main.js";

// ---- timeline -------------------------------------------------------------
// The tracklist is the truth about which tracks exist; the run's probes are
// evidence drawn over it (see web_timeline.js). Everything here is redrawn
// from `tracks` by renderTimeline(), which render() calls, so every existing
// mutation -- edit, reject, insert, save, reload -- keeps the grid current
// without a second update path.
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
      "click a track to inspect · double-click to play from there · alt-click to split it there · S splits at the playhead · click a pin to hear what Shazam heard"
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
  renderTimeline();
}

export function blockHue(i) {
  return TL.hue(TL.identity(S.tracks[i].artist, S.tracks[i].title) || "?");
}

export function renderTimeline() {
  if (!TL) return;
  const D = setDuration();
  const W = TL.windows(S.tracks, D);
  const kept = TL.keptWindows(S.tracks, D);
  const rows = document.getElementById("tl-rows");
  rows.innerHTML = "";
  const shown = replayShown === null ? S.probes : S.probes.slice(0, replayShown);
  const gaps = TL.unheard(S.probes, S.probeDuration || 0);
  const hours = Math.max(1, Math.ceil(D / TL.HOUR));
  for (let h = 0; h < hours; h++) {
    const a = h * TL.HOUR, b = a + TL.HOUR;
    const at = t => ((t - a) / TL.HOUR * 100) + "%";
    const span = d => (d / TL.HOUR * 100) + "%";
    const row = el("div", "tl-hour");
    row.appendChild(el("div", "tl-hl", h + "h"));
    const lanes = el("div", "tl-lanes");
    lanes.dataset.hour = h;

    const ruler = el("div", "tl-ruler");
    for (let m = 10; m < 60; m += 10) {
      const tick = el("span", null, ":" + m);
      tick.style.left = (m / 60 * 100) + "%";
      ruler.appendChild(tick);
    }
    lanes.appendChild(ruler);

    const art = el("div", "tl-art");
    const blocks = el("div", "tl-blocks");
    // One block per track that will be written, spanning its kept window: a
    // merge reads as one block because it is one track. Each merged row stays
    // only as a seam. Seams are not click targets -- merged blips sit seconds
    // apart, a pixel or two at this scale -- so a click lands on the kept block,
    // whose inspector lists what was merged into it, each with an Unmerge.
    for (const w of kept) {
      if (w.end <= a || w.start >= b) continue;
      const i = w.index, t = S.tracks[i];
      const x = Math.max(w.start, a), y = Math.min(w.end, b);
      const seg = el("div", "tl-seg" + (!t.artist && !t.title ? " none" : "") + (t === S.selectedTrack ? " sel" : ""));
      seg.style.setProperty("--h", blockHue(i));
      seg.style.left = at(x);
      seg.style.width = span(y - x);
      seg.dataset.i = i;
      seg.title = trackName(t) + " · " + TL.fmt(w.start) + "–" + TL.fmt(w.end);
      if (y - x > 240) seg.appendChild(el("span", null, TL.cleanTitle(t.title) || t.artist || "Unidentified"));
      blocks.appendChild(seg);
      if ((t.artist || t.title) && w.start >= a && w.start < b) {
        const th = el("div", "tl-thumb" + (t === S.selectedTrack ? " sel" : ""));
        th.style.left = at(w.start);
        th.dataset.i = i;
        th.title = trackName(t) + " · " + TL.fmt(w.start);
        if (t.coverart_url) th.style.backgroundImage = "url('" + artSrc(t.coverart_url).replace(/'/g, "%27") + "')";
        else th.style.backgroundImage = GRADS[i % GRADS.length];
        art.appendChild(th);
      }
    }
    S.tracks.forEach((t, i) => {
      if (!t.rejected || W[i].start < a || W[i].start >= b) return;
      const seam = el("div", "tl-seam" + (t === S.selectedTrack ? " sel" : ""));
      seam.style.left = at(W[i].start);
      seam.dataset.i = i;
      blocks.appendChild(seam);
    });
    for (const [x0, y0] of gaps) {
      if (y0 <= a || x0 >= b) continue;
      const g = el("div", "tl-unheard");
      g.style.left = at(Math.max(x0, a));
      g.style.width = span(Math.min(y0, b) - Math.max(x0, a));
      g.title = "Never sampled: " + TL.fmt(x0) + "–" + TL.fmt(y0);
      blocks.appendChild(g);
    }
    lanes.appendChild(art);
    lanes.appendChild(blocks);

    const pins = el("div", "tl-pins" + (S.canSample ? " can-sample" : ""));
    shown.forEach((p, n) => {
      const mid = p.t + p.window / 2;
      if (mid < a || mid >= b) return;
      const hit = p.artist || p.title;
      const pin = el("div", "tl-pin" + (p.purpose === "refine" ? " refine" : "") + (p.purpose === "manual" ? " manual" : "") + (hit ? "" : " miss")
        + ((replayShown !== null && n === replayShown - 1) || (S.liveMode && replayShown === null && n >= seenProbes && seenProbes) ? " drop" : ""));
      pin.style.left = at(mid);
      pin.dataset.t = p.t;
      pin.title = TL.fmt(p.t) + " · " + p.window + "s " + p.purpose + " · "
        + (hit ? (p.artist || "?") + " — " + (p.title || "?") + (p.confidence ? " (" + p.confidence + ")" : "") : "no match");
      pins.appendChild(pin);
    });
    if (S.liveMode && nextProbe && replayShown === null) {
      const mid = nextProbe.t + nextProbe.window / 2;
      if (mid >= a && mid < b) {
        const pin = el("div", "tl-pin next");
        pin.style.left = at(mid);
        pin.title = "Listening here next (" + nextProbe.purpose + ")";
        pins.appendChild(pin);
      }
    }
    for (const t of pendingSamples) {
      if (t < a || t >= b) continue;
      const pin = el("div", "tl-pin pending");
      pin.style.left = at(t);
      pin.title = "Asking Shazam about " + TL.fmt(t) + "…";
      pins.appendChild(pin);
    }
    lanes.appendChild(pins);

    const head = el("div", "tl-playhead");
    head.hidden = true;
    lanes.appendChild(head);
    row.appendChild(lanes);
    rows.appendChild(row);
  }
  const identified = S.tracks.filter(t => !t.rejected && (t.artist || t.title)).length;
  document.getElementById("tl-stats").textContent =
    TL.fmt(D) + " · " + S.tracks.filter(t => !t.rejected).length + " tracks (" + identified + " identified)"
    + (S.probes.length ? " · " + S.probes.length + " samples" : " · no samples saved for this set");
  document.getElementById("tl-replay").hidden = !S.probes.length;
  updatePlayhead();
  // A replay redraws every few dozen milliseconds; rebuilding the inspector
  // that often would take the focus out of a field the user is typing in.
  if (replayShown === null) {
    // Nor when a sample lands or the audio loads while a name is half typed:
    // a rebuilt input never fires `change`, so the typing would vanish.
    // Only the text inputs: clicking one of the panel's buttons focuses it
    // too, and those clicks are exactly what must rebuild the panel.
    const ae = document.activeElement;
    if (!(ae && ae.tagName === "INPUT" && document.getElementById("insp").contains(ae))) renderInspector();
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

export function selectTrack(t, opts) {
  if (!t) return;
  S.selectedTrack = t;
  document.querySelectorAll("#tl-rows .sel").forEach(n => n.classList.remove("sel"));
  const i = S.tracks.indexOf(t);
  document.querySelectorAll('#tl-rows [data-i="' + i + '"]').forEach(n => n.classList.add("sel"));
  document.querySelectorAll("#list .row.selected").forEach(n => n.classList.remove("selected"));
  const listRow = document.getElementById("list").children[i];
  if (listRow) listRow.classList.add("selected");
  if (opts && opts.reveal) {
    const block = document.querySelector('#tl-rows .tl-blocks [data-i="' + i + '"]');
    if (block) block.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  renderInspector();
}

export const tlRows = document.getElementById("tl-rows");
tlRows.addEventListener("click", e => {
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
  if (!e.target.closest(".tl-blocks")) return;
  const t = timeAt(e);
  if (t !== null) playAt(t);
});
tlRows.addEventListener("mousemove", e => {
  const lanes = e.target.closest(".tl-lanes");
  let cur = tlRows.querySelector(".tl-cursor");
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
