// The selected track's inspector, and asking Shazam about a moment.

import { S, TL } from "./state.js";
import { audio, el, icon, toast } from "./dom.js";
import { begin } from "./activity.js";
import { ask } from "./dialog.js";
import { fmt, parseTime, trackName } from "./util.js";
import { setDuration } from "./model.js";
import { addBelow, renderList } from "./list.js";
import { playAt, playFrom, seekTo } from "./player.js";
import { pendingSamples, renderTimeline } from "./grid.js";
import { mergeNote, mergeSelection, mergeWords, moveStart, restoreSelection, setNames, splitAtPlayhead, startOf, tidyTitles, tidyable,
  toggleMerge } from "./edits.js";
import { artFor, showArtwork } from "./artwork.js";
import { render } from "./main.js";
import { html, Icon, keyOf, render as paint, useEffect, useState } from "./ui.js";
import { clearMulti, isMulti, selection } from "./selection.js";

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

// The track's start (#55): typed as M:SS or H:MM:SS, kept between its
// neighbours like a dragged boundary. Same draft pattern as NameField.
function StartField({ t }) {
  const shown = fmt(startOf(t));
  const [draft, setDraft] = useState(shown);
  useEffect(() => setDraft(shown), [shown]);
  const commit = e => {
    const secs = parseTime(e.target.value);
    if (secs === null) {
      toast("Start times look like 4:05 or 1:04:05");
      setDraft(shown);
      return;
    }
    if (!moveStart(t, secs) && secs !== startOf(t)) toast("It can't start there: it would pass the track next to it");
    setDraft(fmt(startOf(t)));
  };
  const onKeyDown = ev => {
    if (ev.key === "Enter") { ev.preventDefault(); ev.target.blur(); }
    if (ev.key === "Escape") { ev.target.value = shown; setDraft(shown); ev.target.blur(); }
  };
  return html`
    <div class="in-lbl">Start</div>
    <div class="in-start-row">
      <input class="in-start" value=${draft} aria-label="Start" readOnly=${S.liveMode}
        onInput=${e => setDraft(e.target.value)} onChange=${commit} onKeyDown=${onKeyDown} />
      ${!S.liveMode && html`<span class="in-note">or drag the block's left edge · [ and ] nudge it</span>`}
    </div>`;
}

// One of the inspector's actions (#62 follow-up): an icon over a short label,
// in rows that always fill the panel's width, at the top where they're reached
// without scrolling. The full description and the key are in the tooltip.
function Tile({ cls, icon, fill, label, title, disabled, onClick }) {
  return html`<button type="button" class=${"act-tile" + (cls ? " " + cls : "")} disabled=${disabled}
    title=${title} aria-label=${title ? label + ": " + title : label} onClick=${onClick}>
    <${Icon} name=${icon} cls=${fill ? "fill" : ""} /><span>${label}</span></button>`;
}

// Several tracks selected (#77): what they span and what can be done to them
// all, above the inspector of the one the keys act on. That track's own merge
// tiles are left out meanwhile, so each merge on the panel means one thing.
function SelectionPanel() {
  const sel = selection();
  const W = TL.windows(S.tracks, setDuration());
  const from = W[S.tracks.indexOf(sel[0])].start, to = W[S.tracks.indexOf(sel[sel.length - 1])].end;
  const merged = sel.filter(t => t.rejected).length, keptN = sel.length - merged;
  const tidy = tidyable(sel).length;
  const just = () => { clearMulti(); renderList(); renderTimeline(); };
  const prev = mergeWords("prev"), next = mergeWords("next");
  return html`
    <div class="in-sel">
      <div class="in-sel-head">
        <div>
          <div class="in-sel-count">${sel.length + " tracks selected"}</div>
          <div class="in-when">${TL.fmt(from) + " – " + TL.fmt(to) + (merged ? " · " + merged + " merged" : "")}</div>
        </div>
        <button type="button" class="xbtn" title="Select just this track again (Esc)" aria-label="Select just one track"
          onClick=${just}><${Icon} name="x" /></button>
      </div>
      ${!S.liveMode && html`
        <div class="in-acts" role="group" aria-label="Selected tracks">
          ${keptN > 0 && html`<${Tile} icon="merge-up" label="Merge up" title=${prev + " (⌫)"} onClick=${() => mergeSelection("prev")} />`}
          ${keptN > 0 && html`<${Tile} icon="merge-down" label="Merge down" title=${next + " (⇧⌫)"} onClick=${() => mergeSelection("next")} />`}
          ${merged > 0 && html`<${Tile} icon="bring-back" label="Bring back" title=${"Bring the " + merged + " merged back" + (keptN ? "" : " (⌫)")}
            onClick=${restoreSelection} />`}
          ${tidy > 0 && html`<${Tile} icon="eraser" label="Tidy" title=${"Remove the remaster tag from " + (tidy === 1 ? "1 title" : tidy + " titles")}
            onClick=${() => tidyTitles(sel)} />`}
        </div>
        ${keptN > 0 && html`<div class="in-note">${mergeNote()}</div>`}`}
    </div>`;
}

function Inspector({ t, i, multi }) {
  const D = setDuration();
  // A kept track owns everything merged into it, so its span and its samples
  // are the kept window's; a merged row shows its own old span.
  const kept = TL.keptWindows(S.tracks, D);
  const k = kept.findIndex(x => x.index === i);
  const w = k === -1 ? TL.windows(S.tracks, D)[i] : kept[k];
  const joined = TL.joinProbes(S.tracks, S.probes, D)[i];
  const canArt = (t.artist || t.title) && t.index !== undefined && t.index !== null;
  const thumbBg = artFor(t, i);

  // Every name Shazam gave the audio in this window. The neighbours' names are
  // left out: a probe at the edge hearing the next track is not a candidate.
  const neighbours = k === -1 ? [] : [kept[k - 1], kept[k + 1]].filter(Boolean).map(x => S.tracks[x.index]);
  const names = TL.variants(joined, neighbours);
  // What was merged into this track, one Unmerge each: the grid shows these
  // only as seams too close together to click.
  // Rows can be merged in from either side (#56), so this is by owner.
  const owner = TL.owners(S.tracks, D);
  const folded = k === -1 ? [] : S.tracks.filter((m, r) => r !== i && owner[r] === i);
  const dir = owner[i] > i ? "next" : "previous";

  // The split and identify buttons also follow the playhead between renders:
  // grid.updatePlayhead() keeps their `disabled` current as it moves.
  const now = audio.currentTime || 0;
  // Not inside time merged in from before (#56): see edits.splitAt().
  const cantSplit = !(now > Math.max(w.start, t.timestamp) + 1 && now < w.end - 1);
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
          ${t.rejected && html`<span class="badge warn">${owner[i] === -1 ? "rejected" : "merged into " + dir}</span>`}
          ${t._new && html`<span class="badge">new — unsaved</span>`}
          ${t.episode_cover && html`<span class="badge star"><${Icon} name="star" cls="fill" />episode cover</span>`}
          <span class="badge">${joined.length + " sample" + (joined.length === 1 ? "" : "s")}</span>
        </div>
      </div>
    </div>
    <div class="in-acts" role="group" aria-label="Play, identify or split">
      <${Tile} cls="primary" icon="play" fill label="Play" title="Play from its start (⇧Space)"
        onClick=${() => playFrom(i, { scroll: false })} />
      ${!S.liveMode && !t.rejected && html`<${Tile} cls="in-identify" icon="scan" label="Identify"
        disabled=${cantIdentify} title=${identTitle} onClick=${() => identifyAt(t, audio.currentTime || 0)} />`}
      ${!S.liveMode && html`<${Tile} cls="in-split" icon="scissors" label="Split" disabled=${cantSplit}
        title=${cantSplit ? "Play inside this track, then split where the next one starts (S)" : "Start a new track at " + TL.fmt(now) + " (S)"}
        onClick=${splitAtPlayhead} />`}
    </div>
    ${!S.liveMode && !multi && html`
      <div class="in-acts" role="group" aria-label="Track actions">
        ${t.rejected
          ? html`<${Tile} icon="bring-back" label=${owner[i] === -1 ? "Bring back" : "Unmerge"}
              title=${owner[i] === -1 ? "Put it back in the tracklist (⌫)" : "Give it its own time again (⌫)"} onClick=${() => toggleMerge(t)} />`
          : html`
            ${k > 0 && html`<${Tile} icon="merge-up" label="Merge up"
              title="Merge into the previous track: its time goes to the track before (⌫)" onClick=${() => toggleMerge(t, "prev")} />`}
            ${k < kept.length - 1 && html`<${Tile} icon="merge-down" label="Merge down"
              title="Merge into the next track, which starts here instead (⇧⌫)" onClick=${() => toggleMerge(t, "next")} />`}
            ${kept.length === 1 && html`<${Tile} icon="x" label="Reject" title="Leave it out of the tracklist" onClick=${() => toggleMerge(t)} />`}`}
        <${Tile} icon="list-plus" label="Add below" title="Add a track after this one" onClick=${() => addBelow(i)} />
        ${canArt && html`<${Tile} icon="image" label="Artwork" title="Artwork and episode cover" onClick=${() => showArtwork(t)} />`}
      </div>
      <div class="in-note in-hint" hidden=${!(cantSplit && (cantIdentify || t.rejected))}>${t.rejected ? "Split acts at the playhead. Play inside this track to use it."
        : "Identify and Split act at the playhead. Play inside this track to use them."}</div>`}
    <${NameField} t=${t} field="artist" label="Artist" />
    <${NameField} t=${t} field="title" label="Title" />
    <${StartField} t=${t} />
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
    ${t.rejected && html`<div class="in-note">${owner[i] === -1
      ? "Rejected tracks are left out of the tracklist when you save."
      : "Merged tracks are left out of the tracklist when you save; their time goes to the track " + (dir === "next" ? "after, which starts here instead." : "before.")}</div>`}
    ${S.liveMode && html`<div class="in-note">Still identifying — this is the run's current answer, and it may change as the run narrows the boundaries. Editing opens when it finishes.</div>`}`;
}

export function renderInspector() {
  const box = document.getElementById("insp");
  const t = S.selectedTrack;
  const i = t ? S.tracks.indexOf(t) : -1;
  if (i === -1) {
    S.selectedTrack = null;
    paint(html`<div class="blank">
      <div class="blank-ico"><${Icon} name="list-music" /></div>
      <div class="blank-title">No track selected</div>
      <p>Click a track on the timeline — or anything in “Needs a look” — to see what Shazam heard there and fix it.</p>
      <p class="blank-keys"><kbd>↑</kbd><kbd>↓</kbd> move through the list · <kbd>⌘K</kbd> finds a track by name</p>
    </div>`, box);
    return;
  }
  // Keyed by track: choosing another track starts its fields fresh.
  const multi = isMulti();
  paint(html`${multi && html`<${SelectionPanel} />`}<${Inspector} key=${keyOf(t)} t=${t} i=${i} multi=${multi} />`, box);
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
  // In the toolbar while it waits its turn (#62); several read as "· 2 queued".
  const job = begin("shazam", "Asking Shazam about " + TL.fmt(t));
  try {
    const r = await fetch("/api/sample", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ t }) });
    const res = await r.json();
    if (res.ok) {
      S.probes.push(res.probe);
      const p = got = res.probe;
      if (!quiet) toast(TL.fmt(t) + " — " + (p.artist || p.title ? (p.artist || "?") + " — " + (p.title || "?") : "Shazam heard nothing it knew"));
    } else {
      toast("Sample failed: " + (res.error || r.status), { kind: "error" });
    }
  } catch (e) {
    toast("Sample failed: the editor's server did not answer", { kind: "error" });
  } finally {
    job.end();
    pendingSamples.splice(pendingSamples.indexOf(t), 1);
    renderList();  // its Samples column
    renderTimeline();
  }
  return got;
}

// "Replace the name?" as an in-app dialog (#61): both names side by side, the
// moment and the sample's confidence, and a ▶ to hear the ten seconds Shazam
// fingerprinted (the sample is centred on the moment) before deciding.
// Listening stops when the dialog closes and the playhead goes back to where
// it was; without a listen, playback already running is left alone.
function confirmReplace(t, p, at) {
  const body = el("div");
  const dl = el("dl", "ask-compare");
  dl.append(el("dt", null, "Now"), el("dd", null, trackName(t)),
    el("dt", null, "Shazam heard"), el("dd", null, (p.artist || "?") + " — " + (p.title || "?")));
  const meta = el("div", "ask-meta");
  const listen = el("button", "artbtn");
  listen.type = "button";
  let started = false, from = 0;
  const sync = () => {
    const on = started && !audio.paused;
    listen.replaceChildren(icon(on ? "pause" : "play", "fill"), on ? "Pause" : "Listen");
  };
  sync();
  listen.onclick = () => {
    if (started && !audio.paused) { audio.pause(); sync(); return; }
    if (!started) from = audio.currentTime || 0;
    started = true;
    playAt(Math.max(0, at - 5));
    sync();
  };
  audio.addEventListener("play", sync);
  audio.addEventListener("pause", sync);
  meta.append(listen, el("span", null, "at " + TL.fmt(at)
    + (typeof p.confidence === "number" ? " · confidence " + p.confidence.toFixed(2) : "")));
  body.append(dl, meta);
  return ask({
    title: "Replace this track's name?",
    body,
    buttons: [
      { label: "Keep the current name", value: false },
      { label: "Replace", value: true, primary: true },
    ],
    onClose: () => {
      audio.removeEventListener("play", sync);
      audio.removeEventListener("pause", sync);
      if (started) { audio.pause(); seekTo(from); }  // back where the user was
    },
  }).then(v => v === true);
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
  const p = await sampleAt(at, { quiet: true });
  if (!p) return;  // sampleAt already said why
  const heard = (p.artist || "?") + " — " + (p.title || "?");
  if (!p.artist && !p.title) { toast(TL.fmt(at) + " — Shazam heard nothing it knew"); return; }
  if (S.liveMode || !S.tracks.includes(t) || t.rejected) { toast(TL.fmt(at) + " — " + heard); return; }
  if (TL.identity(p.artist, p.title) === TL.identity(t.artist, t.title)) {
    toast("Shazam agrees: " + heard);
    return;
  }
  if ((t.artist || t.title) && !(await confirmReplace(t, p, at))) {
    return;  // still in "Shazam heard", with a Use button
  }
  if (setNames(t, p.artist || "", p.title || "")) {
    S.selectedTrack = t;
    render();
    toast("Named: " + heard + " — ⌘Z to undo");
  }
}
