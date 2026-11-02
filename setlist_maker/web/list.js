// The tracklist pane: rows, inline editing, inserting.

import { GRADS, S } from "./state.js";
import { fmt, parseTime } from "./util.js";
import { reindexPlayingTrack } from "./model.js";
import { setDirty } from "./history.js";
import { playFrom } from "./player.js";
import { artObserver, artSrc, showArtwork } from "./artwork.js";
import { render } from "./main.js";

export function rowEl(t, i) {
  const unknown = !t.artist && !t.title;
  const row = document.createElement("div");
  row.className = "row" + (t.rejected ? " rejected" : "") + (unknown ? " unknown" : "")
    + (t === S.selectedTrack ? " selected" : "");

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  // backgroundImage, never the `background` shorthand: the shorthand resets
  // background-size/position, so the composite would render at natural size
  // anchored top-left -- a corner crop with its label off-screen.
  thumb.style.backgroundImage = GRADS[i % GRADS.length];
  const hasIndex = t.index !== undefined && t.index !== null;
  if (unknown) {
    thumb.textContent = "?";
  } else if (hasIndex) {
    // The gradient stays until the composite lands, so rows never jump.
    thumb.dataset.index = t.index;
    thumb.onclick = (e) => { e.stopPropagation(); showArtwork(t); };
    if (t._art) {
      // Picked in this session and not saved yet. The server's composite is
      // still built from the old art, so requesting it would contradict what
      // the user just chose; show the raw choice instead. "Automatic" leaves
      // no URL to show, so the gradient placeholder stands in until the save.
      if (t.coverart_url) thumb.style.backgroundImage = "url('" + artSrc(t.coverart_url) + "')";
    } else {
      artObserver.observe(thumb);
    }
  }
  // else: an unsaved inserted row has no server index yet -- keep the
  // gradient placeholder and skip the artwork request entirely.
  row.appendChild(thumb);

  const meta = document.createElement("div");
  meta.className = "meta";
  const artistEl = document.createElement("div");
  artistEl.className = "artist";
  artistEl.textContent = t.artist || (unknown ? "Unidentified" : "");
  const titleEl = document.createElement("div");
  titleEl.className = "title";
  titleEl.textContent = t.title || (unknown ? "add a label" : "");
  meta.append(artistEl, titleEl);  // textContent, not innerHTML — no XSS surface
  meta.onclick = () => { if (!S.liveMode) startEdit(row, i); };
  row.appendChild(meta);

  // Passive badge, not a control: the choice is made in the artwork panel,
  // but which track carries it has to be visible without opening every row.
  if (t.episode_cover) {
    const star = document.createElement("div");
    star.className = "star";
    star.textContent = "★";
    star.title = "This track's artwork is the episode cover";
    row.appendChild(star);
  }

  const time = document.createElement("div");
  time.className = "time"; time.textContent = t.time;
  row.appendChild(time);

  const play = document.createElement("button");
  play.className = "iconbtn play"; play.textContent = "▶";
  play.onclick = (e) => { e.stopPropagation(); playFrom(i, { scroll: false }); };
  row.appendChild(play);

  const rej = document.createElement("button");
  rej.className = "iconbtn reject"; rej.textContent = t.rejected ? "↩" : "✕";
  rej.onclick = (e) => {
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
  row.appendChild(rej);

  const add = document.createElement("button");
  add.className = "iconbtn add"; add.textContent = "＋"; add.title = "Add a track below";
  add.onclick = (e) => { e.stopPropagation(); addBelow(i); };
  row.appendChild(add);

  return row;
}

export const INSERT_TAIL_GAP = 30;  // seconds past the last track when adding at the end

// Insert a blank, editable track after row i; default its time to the midpoint
// to the next track (or just past the last one), then open it for editing.
export function addBelow(i) {
  const cur = S.tracks[i], next = S.tracks[i + 1];
  const ts = next ? Math.floor((cur.timestamp + next.timestamp) / 2) : cur.timestamp + INSERT_TAIL_GAP;
  S.tracks.splice(i + 1, 0, { artist: "", title: "", timestamp: ts, time: fmt(ts), rejected: false, _new: true });
  reindexPlayingTrack();  // the splice can shift the playing track's index
  setDirty(true);
  render();
  const row = document.getElementById("list").children[i + 1];
  if (row) startEdit(row, i + 1);
}

export function startEdit(row, i) {
  if (row.querySelector(".editfields")) return;
  const t = S.tracks[i];
  const isNew = !!t._new;
  const meta = row.querySelector(".meta");
  row.classList.add("editing");
  const wrap = document.createElement("div");
  wrap.className = "editfields";
  const a = document.createElement("input"); a.value = t.artist; a.placeholder = "Artist";
  const ti = document.createElement("input"); ti.value = t.title; ti.placeholder = "Title";
  wrap.appendChild(a); wrap.appendChild(ti);
  // Inserted rows also edit their timestamp; existing rows keep title/artist only.
  let tm = null;
  if (isNew) {
    tm = document.createElement("input");
    tm.className = "timefield"; tm.value = t.time || fmt(t.timestamp); tm.placeholder = "MM:SS";
    wrap.appendChild(tm);
  }
  meta.replaceWith(wrap);
  a.focus();
  function commit() {
    const na = a.value.trim(), nt = ti.value.trim();
    if (na !== t.artist || nt !== t.title) { t.artist = na; t.title = nt; if (!isNew) t._edited = true; setDirty(true); }
    if (tm) {
      const secs = parseTime(tm.value);
      if (secs !== null && secs !== t.timestamp) {
        t.timestamp = secs; setDirty(true);
        S.tracks.sort((x, y) => x.timestamp - y.timestamp);
        reindexPlayingTrack();  // the re-sort can shift the playing track's index
      }
      t.time = fmt(t.timestamp);
    }
    render();
  }
  a.addEventListener("keydown", ev => { if (ev.key === "Enter") ti.focus(); if (ev.key === "Escape") render(); });
  ti.addEventListener("keydown", ev => { if (ev.key === "Enter") { if (tm) tm.focus(); else commit(); } if (ev.key === "Escape") render(); });
  if (tm) tm.addEventListener("keydown", ev => { if (ev.key === "Enter") commit(); if (ev.key === "Escape") render(); });
  wrap.addEventListener("focusout", () => { setTimeout(() => { if (!wrap.contains(document.activeElement)) commit(); }, 0); });
}

// The highlight must be reapplied after render(), which rebuilds every row.
export function highlightPlayingRow(scroll) {
  const list = document.getElementById("list");
  list.querySelectorAll(".row.playing").forEach(el => el.classList.remove("playing"));
  if (S.playingIndex === null) return;
  const row = list.children[S.playingIndex];
  if (!row) return;
  row.classList.add("playing");
  if (scroll) row.scrollIntoView({ block: "center", behavior: "smooth" });
}
