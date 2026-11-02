// The tracklist pane: rows, inline editing, inserting.
//
// A table: one Preact render of every row, keyed by track object, so an edit,
// insert or re-sort updates the rows that changed and moves the rest.
// Selection, the playing row and the row being edited are all derived from S
// on each render; nothing reaches into a row's DOM afterwards. The column
// header is static markup beside #list, not inside it: this render owns all
// of #list and would replace anything else put there.

import { S, TL } from "./state.js";
import { fmt, parseTime } from "./util.js";
import { clampStart, playingIndex, setDuration } from "./model.js";
import { setDirty } from "./history.js";
import { toast } from "./dom.js";
import { playFrom } from "./player.js";
import { artFor, showArtwork, whenVisible } from "./artwork.js";
import { render } from "./main.js";
import { selectTrack } from "./grid.js";
import { previewStart, startOf, toggleMerge } from "./edits.js";
import { html, keyOf, render as paint, useEffect, useRef, useState } from "./ui.js";

const listEl = document.getElementById("list");

// The row's thumbnail: artFor(), the same picture as everywhere else (#60).
// The cover is asked for only once the row is near the viewport, since a
// set opened for the first time is network-bound on every lookup.
function Thumb({ t, i }) {
  const unknown = !t.artist && !t.title;
  const hasIndex = t.index !== undefined && t.index !== null;
  const [near, setNear] = useState(false);
  const ref = useRef(null);
  useEffect(() => near ? undefined : whenVisible(ref.current, () => setNear(true)), [near]);
  // backgroundImage, never the `background` shorthand: the shorthand resets
  // background-size/position, so the cover would render at natural size
  // anchored top-left.
  const bg = artFor(t, i, near);
  const open = hasIndex && !unknown ? e => { e.stopPropagation(); showArtwork(t); } : undefined;
  return html`<div class="thumb" ref=${ref} style=${{ backgroundImage: bg }} onClick=${open}>
    ${unknown ? "?" : ""}</div>`;
}

// Inline artist, title and start fields. The typed text lives in component
// state, so a re-render underneath -- the playing row moving on, a sample
// landing -- keeps it. Enter moves on and finally commits; Escape cancels;
// leaving the fields commits. An inserted row's start may land anywhere and
// is sorted into place; an existing track's is kept between its neighbours
// (clampStart), like a dragged boundary, so the list never reorders under it.
function EditFields({ t }) {
  const isNew = !!t._new;
  const [artist, setArtist] = useState(t.artist);
  const [title, setTitle] = useState(t.title);
  // Where the track starts as written, which a merge into it from before
  // moves back to that row's start (#56).
  const start = startOf(t);
  const startText = () => start === t.timestamp ? (t.time || fmt(start)) : fmt(start);
  const [time, setTime] = useState(startText);
  // A start moved elsewhere while this is open (a drag, [ or ]) is the new
  // value; committing the one seeded at mount would move the track back.
  useEffect(() => setTime(startText()), [start]);
  const wrap = useRef(null), artistIn = useRef(null), titleIn = useRef(null), timeIn = useRef(null);
  // Closing runs once: Enter commits and unmounts the fields, and the
  // focusout that follows must not commit a second time.
  const closed = useRef(false);
  // Into whichever cell was clicked.
  // From the keyboard (Enter, F2) its text is selected, so typing replaces
  // the name, as renaming does in a desktop list; a click puts the caret there.
  useEffect(() => {
    const field = ({ title: titleIn, time: timeIn }[S.editField] || artistIn).current;
    field.focus();
    if (S.editSelect) field.select();
  }, []);

  // Enter and Escape hand focus back to the row, so the arrows walk on from
  // it; leaving the fields by clicking elsewhere leaves focus there.
  const close = (refocus) => {
    closed.current = true;
    if (S.editing === t) S.editing = null;
    render();
    if (refocus) focusRow(t);
  };
  const commit = (refocus) => {
    if (closed.current) return;
    const na = artist.trim(), nt = title.trim();
    if (na !== t.artist || nt !== t.title) {
      t.artist = na; t.title = nt;
      if (!isNew) t._edited = true;
      setDirty(true);
    }
    const secs = parseTime(time);
    if (secs === null && time.trim() !== t.time) toast("Start times look like 4:05 or 1:04:05");
    if (isNew) {
      if (secs !== null && secs !== t.timestamp) {
        t.timestamp = secs; setDirty(true);
        S.tracks.sort((x, y) => x.timestamp - y.timestamp);
      }
    } else if (secs !== null && secs !== start) {
      // The row holding the start moves: the track's own, or the first one
      // merged into it from before.
      const i = TL.startIndex(S.tracks, S.tracks.indexOf(t));
      const row = S.tracks[i];
      const at = clampStart(i, secs);
      if (at !== row.timestamp) { previewStart(row, at); setDirty(true); }
      if (at !== secs) toast("Kept between its neighbours: starts at " + fmt(at));
    }
    if (isNew) t.time = fmt(t.timestamp);
    close(refocus);
  };
  // Escape used to rebuild the list, which removed the focused input; Chrome
  // fires focusout for that, so the "cancel" committed what had been typed.
  const cancel = () => { if (!closed.current) close(true); };
  const keys = next => ev => {
    if (ev.key === "Escape") { ev.preventDefault(); cancel(); }
    if (ev.key === "Enter") { ev.preventDefault(); if (next && next.current) { next.current.focus(); next.current.select(); } else commit(true); }
  };
  const onFocusOut = () => setTimeout(() => {
    if (wrap.current && !wrap.current.contains(document.activeElement)) commit(false);
  }, 0);
  return html`
    <div class="editfields" ref=${wrap} onfocusout=${onFocusOut}>
      <input ref=${artistIn} value=${artist} placeholder="Artist"
        onInput=${e => setArtist(e.target.value)} onKeyDown=${keys(titleIn)} />
      <input ref=${titleIn} value=${title} placeholder="Title"
        onInput=${e => setTitle(e.target.value)} onKeyDown=${keys(timeIn)} />
      <input ref=${timeIn} class="timefield" value=${time} placeholder="MM:SS" aria-label="Start"
        onInput=${e => setTime(e.target.value)} onKeyDown=${keys(null)} />
    </div>`;
}

function Row({ t, i, playing, span, samples, tab }) {
  const unknown = !t.artist && !t.title;
  const editing = S.editing === t;
  const selected = t === S.selectedTrack;
  const cls = "row trow" + (t.rejected ? " rejected" : "") + (unknown ? " unknown" : "")
    + (selected ? " selected" : "") + (playing ? " playing" : "")
    + (editing ? " editing" : "");
  // ✕ merges into the previous track, as it always has; ↩ brings back a row
  // merged either way. edits.mergeTrack() owns the rules (the star, #56).
  const reject = e => { e.stopPropagation(); toggleMerge(t); };
  // Clicking a row selects it; clicking its artist or title also edits it in
  // place. The .meta wrapper is display:contents, so its two cells sit in the
  // row's grid while still sharing one click handler.
  return html`
    <div class=${cls} data-key=${keyOf(t)} role="option" aria-selected=${selected ? "true" : "false"}
      aria-label=${rowLabel(t, i)} tabindex=${tab ? 0 : -1} onClick=${() => selectTrack(t)}>
      <span class="c-num idx">${i + 1}</span>
      <${Thumb} t=${t} i=${i} />
      ${editing
        ? html`<${EditFields} t=${t} />`
        : html`<div class="meta">
            <div class="artist" onClick=${() => startEdit(t, "artist")}>${t.artist || (unknown ? "Unidentified" : "")}</div>
            <div class="title" onClick=${() => startEdit(t, "title")}>${t.title || (unknown ? "add a label" : "")}</div>
          </div>`}
      ${!editing && html`<span class="c-num time" onClick=${() => startEdit(t, "time")}>${startOf(t) === t.timestamp ? t.time : fmt(startOf(t))}</span>`}
      <span class="c-num len">${span === null ? "—" : fmt(span)}</span>
      <span class="c-num smp">${samples || ""}</span>
      <span class="star" title=${t.episode_cover ? "This track's artwork is the episode cover" : undefined}>${t.episode_cover ? "★" : ""}</span>
      <span class="acts">
        <button class="iconbtn play" tabindex="-1" title="Play from here" onClick=${e => { e.stopPropagation(); playFrom(i, { scroll: false }); }}>▶</button>
        <button class="iconbtn reject" tabindex="-1" title=${t.rejected ? "Bring back" : "Reject"} onClick=${reject}>${t.rejected ? "↩" : "✕"}</button>
        <button class="iconbtn add" tabindex="-1" title="Add a track below"
          onClick=${e => { e.stopPropagation(); addBelow(i); }}>＋</button>
      </span>
    </div>`;
}

// What a screen reader announces for a row: its place, name and start.
function rowLabel(t, i) {
  const name = t.artist || t.title ? (t.artist || "Unknown artist") + (t.title ? ", " + t.title : "") : "Unidentified";
  return (i + 1) + ". " + name + ", " + fmt(startOf(t)) + (t.rejected ? ", merged" : "");
}

export function renderList() {
  const p = playingIndex();
  // Roving tabindex (#58): the selected row is the list's one tab stop, or
  // the first row while nothing is selected.
  const tabAt = Math.max(0, S.tracks.indexOf(S.selectedTrack));
  const D = setDuration();
  // Length is what will be written: a kept track's kept window, which runs
  // over any rows merged into it. A merged row has none of its own.
  // Without /timeline.js the grid is gone, but the list must still render.
  const spans = S.tracks.map(() => null);
  if (TL) for (const w of TL.keptWindows(S.tracks, D)) spans[w.index] = w.end - w.start;
  const joined = TL ? TL.joinProbes(S.tracks, S.probes, D) : null;
  paint(S.tracks.map((t, i) => html`<${Row} key=${keyOf(t)} t=${t} i=${i} playing=${i === p}
    tab=${i === tabAt} span=${spans[i]} samples=${joined ? joined[i].length : 0} />`), listEl);
}

// Bring the playing row into view (automatic advance, prev/next).
export function scrollToPlaying() {
  const row = listEl.querySelector(".row.playing");
  if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
}

// Bring the selected row into view, when it was chosen somewhere else.
export function revealSelected() {
  const row = listEl.querySelector(".row.selected");
  if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// Keyboard selection moves focus with it, so the list keeps one tab stop
// where the user is. Scrolling is revealSelected()'s ("nearest"), not focus's.
export function focusSelected() { focusRow(S.selectedTrack); }
function focusRow(t) {
  const row = t && listEl.querySelector('.row[data-key="' + keyOf(t) + '"]');
  if (row) row.focus({ preventScroll: true });
}

export function startEdit(t, field, opts) {
  if (S.liveMode || S.editing === t) return;
  S.editing = t;
  S.editField = field || "artist";
  S.editSelect = !!(opts && opts.select);
  render();
}

export const INSERT_TAIL_GAP = 30;  // seconds past the last track when adding at the end

// Insert a blank, editable track after row i; default its time to the midpoint
// to the next track (or just past the last one), then open it for editing.
export function addBelow(i) {
  const cur = S.tracks[i], next = S.tracks[i + 1];
  const ts = next ? Math.floor((cur.timestamp + next.timestamp) / 2) : cur.timestamp + INSERT_TAIL_GAP;
  const row = { artist: "", title: "", timestamp: ts, time: fmt(ts), rejected: false, _new: true };
  S.tracks.splice(i + 1, 0, row);
  setDirty(true);
  S.editing = row;
  S.editField = "artist";
  render();
}
