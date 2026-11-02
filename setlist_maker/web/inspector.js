// The selected track's inspector, and asking Shazam about a moment.

import { GRADS, S, TL } from "./state.js";
import { audio, toast } from "./dom.js";
import { trackName } from "./util.js";
import { setDuration } from "./model.js";
import { addBelow } from "./list.js";
import { playAt, playFrom } from "./player.js";
import { pendingSamples, renderTimeline } from "./grid.js";
import { setNames, splitAtPlayhead, toggleMerge } from "./edits.js";
import { artSrc, showArtwork } from "./artwork.js";
import { render } from "./main.js";
import { html, keyOf, render as paint, useEffect, useState } from "./ui.js";

// A name field. The typed text is component state, so the panel can re-render
// underneath it (a sample landing, the playhead crossing into another track)
// without losing it -- what the old "don't rebuild while an input has focus"
// guard used to protect. It follows the track's own value when that changes
// from elsewhere (Use, undo, a Tidy). Commits on change, i.e. Enter or blur.
function NameField({ t, field, label }) {
  const [draft, setDraft] = useState(t[field]);
  useEffect(() => setDraft(t[field]), [t[field]]);
  const commit = e => {
    const v = e.target.value;
    const artist = field === "artist" ? v : t.artist, title = field === "title" ? v : t.title;
    if (setNames(t, artist, title)) render();
  };
  const onKeyDown = ev => {
    if (ev.key === "Enter") { ev.preventDefault(); ev.target.blur(); }
    if (ev.key === "Escape") {
      // The DOM value first: the blur fires `change` before the state lands.
      ev.target.value = t[field];
      setDraft(t[field]);
      ev.target.blur();
    }
  };
  return html`
    <div class="in-lbl">${label}</div>
    <input class=${"in-" + field} value=${draft} placeholder=${label} aria-label=${label}
      readOnly=${S.liveMode} onInput=${e => setDraft(e.target.value)} onChange=${commit}
      onKeyDown=${onKeyDown} />`;
}

function Inspector({ t, i }) {
  const D = setDuration();
  // A kept track owns everything merged into it, so its span and its samples
  // are the kept window's; a merged row shows its own old span.
  const kept = TL.keptWindows(S.tracks, D);
  const k = kept.findIndex(x => x.index === i);
  const w = k === -1 ? TL.windows(S.tracks, D)[i] : kept[k];
  const joined = TL.joinProbes(S.tracks, S.probes, D)[i];
  const canArt = (t.artist || t.title) && t.index !== undefined && t.index !== null;
  const thumbBg = t.coverart_url
    ? "url('" + artSrc(t.coverart_url).replace(/'/g, "%27") + "')" : GRADS[i % GRADS.length];

  // Every name Shazam gave the audio in this window. The neighbours' names are
  // left out: a probe at the edge hearing the next track is not a candidate.
  const neighbours = k === -1 ? [] : [kept[k - 1], kept[k + 1]].filter(Boolean).map(x => S.tracks[x.index]);
  const names = TL.variants(joined, neighbours);
  // What was merged into this track, one Unmerge each: the grid shows these
  // only as seams too close together to click.
  const folded = k === -1 ? [] : S.tracks.slice(i + 1, kept[k + 1] ? kept[k + 1].index : S.tracks.length);

  // The split and identify buttons also follow the playhead between renders:
  // grid.updatePlayhead() keeps their `disabled` current as it moves.
  const now = audio.currentTime || 0;
  const cantSplit = !(now > w.start + 1 && now < w.end - 1);
  const cantIdentify = !S.canSample || !(now >= w.start && now < w.end);
  const identTitle = !S.canSample ? "Sampling needs the set's audio"
    : cantIdentify ? "Play inside this track, then ask Shazam what is playing (I)"
    : "Ask Shazam what is playing at " + TL.fmt(now) + " and name this track (I)";
  const sample = p => {
    const hit = p.artist || p.title;
    return html`
      <button type="button" title=${p.purpose + (p.confidence ? " · confidence " + p.confidence : "")}
        onClick=${() => playAt(p.t)}>
        <span>${TL.fmt(p.t)}</span>
        <span class=${"nm" + (hit ? "" : " miss")}>${hit ? (p.artist || "?") + " — " + (p.title || "?") : "no match"}</span>
        <span>${p.purpose === "refine" ? "12s" : p.window + "s"}</span>
      </button>`;
  };

  return html`
    <div class="in-hero">
      <div class="thumb" style=${{ backgroundImage: thumbBg, cursor: canArt ? "" : "default" }}
        title=${canArt ? "Artwork and episode cover" : undefined}
        onClick=${canArt ? () => showArtwork(t) : undefined}></div>
      <div style=${{ minWidth: "0" }}>
        <div class="in-when">${TL.fmt(w.start) + " – " + TL.fmt(w.end) + " · " + TL.fmt(w.end - w.start)}</div>
        <div class="in-when">${"track " + (i + 1) + " of " + S.tracks.length}</div>
        <div class="in-badges">
          ${t.rejected && html`<span class="badge warn">merged into previous</span>`}
          ${t._new && html`<span class="badge">new — unsaved</span>`}
          ${t.episode_cover && html`<span class="badge">★ episode cover</span>`}
          <span class="badge">${joined.length + " sample" + (joined.length === 1 ? "" : "s")}</span>
        </div>
      </div>
    </div>
    <${NameField} t=${t} field="artist" label="Artist" />
    <${NameField} t=${t} field="title" label="Title" />
    ${names.length > 0 && html`
      <div class="in-lbl">Shazam heard</div>
      ${names.map(v => {
        const current = v.artist === t.artist && v.title === t.title;
        return html`
          <div class="in-variant" key=${v.artist + "\u0000" + v.title}>
            <div class="nm" title=${v.artist + " — " + v.title}>${v.artist + " — " + v.title}</div>
            <span class="ct">${"×" + v.count}</span>
            <button class="tbtn" disabled=${current || S.liveMode}
              onClick=${() => { if (setNames(t, v.artist, v.title)) render(); }}>${current ? "in use" : "Use"}</button>
          </div>`;
      })}`}
    ${joined.length > 0 && html`
      <div class="in-lbl">Samples — click to hear one</div>
      <div class="in-samples">${joined.map(sample)}</div>`}
    ${folded.length > 0 && html`
      <div class="in-lbl">Merged into this track</div>
      ${folded.map(m => html`
        <div class="in-variant" key=${keyOf(m)}>
          <div class="nm" title=${trackName(m)}>${TL.fmt(m.timestamp) + " " + trackName(m)}</div>
          <button class="tbtn" disabled=${S.liveMode} onClick=${() => toggleMerge(m)}>Unmerge</button>
        </div>`)}`}
    <div class="in-btns">
      <button class="tbtn primary" onClick=${() => playFrom(i, { scroll: false })}>▶ Play</button>
      ${!S.liveMode && html`
        <button class="tbtn in-identify" disabled=${cantIdentify} title=${identTitle}
          hidden=${!!t.rejected} onClick=${() => identifyAt(t, audio.currentTime || 0)}>Identify at playhead</button>
        <button class="tbtn in-split" disabled=${cantSplit}
          title=${cantSplit ? "Play inside this track, then split where the next one starts" : "Start a new track at " + TL.fmt(now)}
          onClick=${splitAtPlayhead}>Split at playhead</button>
        <button class="tbtn" onClick=${() => toggleMerge(t)}>${t.rejected ? "Unmerge" : (i === 0 ? "Reject" : "Merge into previous")}</button>
        <button class="tbtn" onClick=${() => addBelow(i)}>＋ Add below</button>`}
      ${canArt && !S.liveMode && html`<button class="tbtn" onClick=${() => showArtwork(t)}>Artwork…</button>`}
    </div>
    ${t.rejected && html`<div class="in-note">Merged tracks are left out of the tracklist when you save; their time goes to the track before.</div>`}
    ${S.liveMode && html`<div class="in-note">Still identifying — this is the run's current answer, and it may change as the run narrows the boundaries. Editing opens when it finishes.</div>`}`;
}

export function renderInspector() {
  const box = document.getElementById("insp");
  const t = S.selectedTrack;
  const i = t ? S.tracks.indexOf(t) : -1;
  if (i === -1) {
    S.selectedTrack = null;
    paint(html`<h3>Track</h3><div class="empty">Click a track on the timeline — or anything in “Needs a look” — to see what Shazam heard there and fix it.</div>`, box);
    return;
  }
  // Keyed by track: choosing another track starts its fields fresh.
  paint(html`<${Inspector} key=${keyOf(t)} t=${t} i=${i} />`, box);
}

// ---- needs a look ----
// Ask Shazam about one moment. The pin shows as pending while the server
// waits its turn (it paces lookups like an identify run) and then lands as
// a real probe, already saved to the run's progress file.
export async function sampleAt(t, opts) {
  const quiet = opts && opts.quiet;
  let got = null;
  pendingSamples.push(t);
  renderTimeline();
  try {
    const r = await fetch("/api/sample", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ t }) });
    const res = await r.json();
    if (res.ok) {
      S.probes.push(res.probe);
      const p = got = res.probe;
      if (!quiet) toast(TL.fmt(t) + " — " + (p.artist || p.title ? (p.artist || "?") + " — " + (p.title || "?") : "Shazam heard nothing it knew"));
    } else {
      toast("Sample failed: " + (res.error || r.status));
    }
  } catch (e) {
    toast("Sample failed");
  } finally {
    pendingSamples.splice(pendingSamples.indexOf(t), 1);
    renderTimeline();
  }
  return got;
}

// Ask Shazam what is playing at `seconds` and name `t` with the answer: a
// sample centred on the moment (so the fingerprinted ten seconds are the
// ones around the playhead), then an ordinary edit -- undoable, unsaved until
// Save. The track is captured now, by reference, because the answer can take
// a pacing interval to arrive. An unidentified track is filled in outright;
// a named one is only replaced after a confirm, since Shazam is often wrong
// mid-mix and the name may be the user's own correction.
export async function identifyAt(t, seconds) {
  if (!S.canSample) { toast("Sampling needs the set's audio"); return; }
  if (!t) return;
  const at = Math.round(seconds);
  toast("Asking Shazam about " + TL.fmt(at) + "…");
  const p = await sampleAt(at, { quiet: true });
  if (!p) return;  // sampleAt already said why
  const heard = (p.artist || "?") + " — " + (p.title || "?");
  if (!p.artist && !p.title) { toast(TL.fmt(at) + " — Shazam heard nothing it knew"); return; }
  if (S.liveMode || !S.tracks.includes(t) || t.rejected) { toast(TL.fmt(at) + " — " + heard); return; }
  if (TL.identity(p.artist, p.title) === TL.identity(t.artist, t.title)) {
    toast("Shazam agrees: " + heard);
    return;
  }
  if ((t.artist || t.title)
      && !confirm("Shazam heard “" + heard + "” at " + TL.fmt(at) + ".\n\nReplace “" + trackName(t) + "”?")) {
    return;  // still in "Shazam heard", with a Use button
  }
  if (setNames(t, p.artist || "", p.title || "")) {
    S.selectedTrack = t;
    render();
    toast("Named: " + heard + " — ⌘Z to undo");
  }
}
