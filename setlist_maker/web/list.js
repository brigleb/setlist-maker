// The tracklist pane: rows, inline editing, inserting.
//
// One Preact render of every row, keyed by track object, so an edit, insert or
// re-sort updates the rows that changed and moves the rest. Selection, the
// playing row and the row being edited are all derived from S on each render;
// nothing reaches into a row's DOM afterwards.

import { GRADS, S } from "./state.js";
import { fmt, parseTime } from "./util.js";
import { playingIndex } from "./model.js";
import { setDirty } from "./history.js";
import { playFrom } from "./player.js";
import { artSrc, showArtwork, whenVisible } from "./artwork.js";
import { render } from "./main.js";
import { html, keyOf, render as paint, useEffect, useRef, useState } from "./ui.js";

const listEl = document.getElementById("list");

// The row's thumbnail: a gradient until the chapter composite has loaded.
// Requested only once the row is near the viewport (sets are network-bound on
// a miss), and again whenever its URL changes -- an edit plus a save bumps
// S.artVersion, which is what makes the browser fetch the regenerated image.
function Thumb({ t, i }) {
  const unknown = !t.artist && !t.title;
  // 0 is a real index; an unsaved inserted row has none and requests nothing.
  const hasIndex = t.index !== undefined && t.index !== null;
  const src = !unknown && hasIndex && !t._art
    ? "/api/artwork?index=" + t.index + "&v=" + S.artVersion : null;
  const [loaded, setLoaded] = useState(null);
  const ref = useRef(null);
  useEffect(() => {
    if (!src) return undefined;
    return whenVisible(ref.current, () => {
      const img = new Image();
      // 404 = unidentified or out of range: the gradient stays.
      img.onload = () => setLoaded({ src, url: img.src });
      img.src = src;
    });
  }, [src]);
  // backgroundImage, never the `background` shorthand: the shorthand resets
  // background-size/position, so the composite would render at natural size
  // anchored top-left -- a corner crop with its label off-screen.
  let bg = GRADS[i % GRADS.length];
  if (t._art) {
    // Picked in this session and not saved yet. The server's composite is
    // still built from the old art, so show the raw choice instead; "Automatic"
    // leaves no URL to show, so the gradient stands in until the save.
    if (t.coverart_url) bg = "url('" + artSrc(t.coverart_url) + "')";
  } else if (loaded && loaded.src === src) {
    bg = "url('" + loaded.url + "')";
  }
  const open = hasIndex && !unknown ? e => { e.stopPropagation(); showArtwork(t); } : undefined;
  return html`<div class="thumb" ref=${ref} style=${{ backgroundImage: bg }} onClick=${open}>
    ${unknown ? "?" : ""}</div>`;
}

// Inline artist/title fields (and the time, for an inserted row). The typed
// text lives in component state, so a re-render underneath -- the playing row
// moving on, a sample landing -- keeps it. Enter moves on and finally commits;
// Escape cancels; leaving the fields commits.
function EditFields({ t }) {
  const isNew = !!t._new;
  const [artist, setArtist] = useState(t.artist);
  const [title, setTitle] = useState(t.title);
  const [time, setTime] = useState(t.time || fmt(t.timestamp));
  const wrap = useRef(null), artistIn = useRef(null), titleIn = useRef(null), timeIn = useRef(null);
  // Closing runs once: Enter commits and unmounts the fields, and the
  // focusout that follows must not commit a second time.
  const closed = useRef(false);
  useEffect(() => { artistIn.current.focus(); }, []);

  const close = () => {
    closed.current = true;
    if (S.editing === t) S.editing = null;
    render();
  };
  const commit = () => {
    if (closed.current) return;
    const na = artist.trim(), nt = title.trim();
    if (na !== t.artist || nt !== t.title) {
      t.artist = na; t.title = nt;
      if (!isNew) t._edited = true;
      setDirty(true);
    }
    if (isNew) {
      const secs = parseTime(time);
      if (secs !== null && secs !== t.timestamp) {
        t.timestamp = secs; setDirty(true);
        S.tracks.sort((x, y) => x.timestamp - y.timestamp);
      }
      t.time = fmt(t.timestamp);
    }
    close();
  };
  // Escape used to rebuild the list, which removed the focused input; Chrome
  // fires focusout for that, so the "cancel" committed what had been typed.
  const cancel = () => { if (!closed.current) close(); };
  const keys = next => ev => {
    if (ev.key === "Escape") { ev.preventDefault(); cancel(); }
    if (ev.key === "Enter") { ev.preventDefault(); if (next && next.current) next.current.focus(); else commit(); }
  };
  const onFocusOut = () => setTimeout(() => {
    if (wrap.current && !wrap.current.contains(document.activeElement)) commit();
  }, 0);
  return html`
    <div class="editfields" ref=${wrap} onfocusout=${onFocusOut}>
      <input ref=${artistIn} value=${artist} placeholder="Artist"
        onInput=${e => setArtist(e.target.value)} onKeyDown=${keys(titleIn)} />
      <input ref=${titleIn} value=${title} placeholder="Title"
        onInput=${e => setTitle(e.target.value)} onKeyDown=${keys(isNew ? timeIn : null)} />
      ${isNew && html`<input ref=${timeIn} class="timefield" value=${time} placeholder="MM:SS"
        onInput=${e => setTime(e.target.value)} onKeyDown=${keys(null)} />`}
    </div>`;
}

function Row({ t, i, playing }) {
  const unknown = !t.artist && !t.title;
  const editing = S.editing === t;
  const cls = "row" + (t.rejected ? " rejected" : "") + (unknown ? " unknown" : "")
    + (t === S.selectedTrack ? " selected" : "") + (playing ? " playing" : "")
    + (editing ? " editing" : "");
  const reject = e => {
    e.stopPropagation();
    t.rejected = !t.rejected;
    // A rejected track is absent from the output entirely -- including from
    // the sidecar, which is where the star is stored -- so it cannot be the
    // episode cover. Clearing it here keeps the ★ from lingering on a row
    // whose choice would silently vanish on the next reload.
    if (t.rejected) t.episode_cover = false;
    setDirty(true);
    render();
  };
  return html`
    <div class=${cls} data-key=${keyOf(t)}>
      <${Thumb} t=${t} i=${i} />
      ${editing
        ? html`<${EditFields} t=${t} />`
        : html`<div class="meta" onClick=${() => startEdit(t)}>
            <div class="artist">${t.artist || (unknown ? "Unidentified" : "")}</div>
            <div class="title">${t.title || (unknown ? "add a label" : "")}</div>
          </div>`}
      ${t.episode_cover && html`<div class="star" title="This track's artwork is the episode cover">★</div>`}
      <div class="time">${t.time}</div>
      <button class="iconbtn play" onClick=${e => { e.stopPropagation(); playFrom(i, { scroll: false }); }}>▶</button>
      <button class="iconbtn reject" onClick=${reject}>${t.rejected ? "↩" : "✕"}</button>
      <button class="iconbtn add" title="Add a track below"
        onClick=${e => { e.stopPropagation(); addBelow(i); }}>＋</button>
    </div>`;
}

export function renderList() {
  const p = playingIndex();
  paint(S.tracks.map((t, i) => html`<${Row} key=${keyOf(t)} t=${t} i=${i} playing=${i === p} />`), listEl);
}

// Bring the playing row into view (automatic advance, prev/next).
export function scrollToPlaying() {
  const row = listEl.querySelector(".row.playing");
  if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
}

export function startEdit(t) {
  if (S.liveMode || S.editing === t) return;
  S.editing = t;
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
  render();
}
