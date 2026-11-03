// Row thumbnails, the artwork panel and picker, uploads, the episode cover, Embed.

import { GRADS, S, TL } from "./state.js";
import { audio, epArtist, epTitle, toast } from "./dom.js";
import { trackName } from "./util.js";
import { setDirty } from "./history.js";
import { render, save } from "./main.js";
import { keepFocus, sheet } from "./dialog.js";
import { startOf } from "./edits.js";
import { selectTrack } from "./grid.js";

// Generate visible rows first: 60-track sets are network-bound on a miss.
// One observer for every thumb; whenVisible() returns the unsubscribe a
// component's effect cleanup calls, so an unmounted row leaves nothing behind.
const onVisible = new WeakMap();
const artObserver = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const cb = onVisible.get(e.target);
    artObserver.unobserve(e.target);
    onVisible.delete(e.target);
    if (cb) cb();
  }
}, { rootMargin: "200px" });  // start just before the row scrolls into view

export function whenVisible(node, cb) {
  onVisible.set(node, cb);
  artObserver.observe(node);
  return () => { artObserver.unobserve(node); onVisible.delete(node); };
}

// The artwork dialog (#61). `artDialog.open` is the one "is it showing?" test.
export const artDialog = sheet(document.getElementById("art-dialog"));
export const overlayImg = document.getElementById("artwork-large");
export const artNote = document.getElementById("artwork-note");
export const artGrid = document.getElementById("artwork-grid");
export const artUrlEl = document.getElementById("art-url");
export const artCoverBtn = document.getElementById("art-cover");
// The track the panel is showing, held as an object reference rather than a
// position: addBelow()/commit() can re-sort the array while the panel is open.
export let artTrack = null;
// Which tile the grid marks as in use. Seeded from the server's own `current`
// flag rather than compared against artTrack.coverart_url: the server offers
// the in-use cover at the size the picker uses, so a Shazam URL saved at 400px
// arrives as the 600px one, and comparing raw strings marks nothing.
export let artCurrentUrl = null;
export let artProbeSeq = 0;   // orders overlapping paste-a-URL probes; see useTypedUrl

export function setPickNote(url) {
  artNote.textContent = url
    ? "Chosen — Save to apply it to the chapter image."
    : "Back to automatic — Save to look the artwork up again.";
  artNote.hidden = false;
}

// Which tracks have artwork to curate: the same rule as the list's thumb and
// the inspector's button -- saved (so the server can answer for its index)
// and named. Rejected tracks are left out of the chapters, so out of the walk.
export const hasArtwork = t => t.index !== undefined && t.index !== null && !!(t.artist || t.title);
const walkable = t => hasArtwork(t) && !t.rejected;

export function showArtwork(t) {
  artTrack = t;
  artUrlEl.value = "";
  if (t._art) {
    // An unsaved pick has to survive closing and reopening the panel. The
    // server composite is still built from saved state, so showing it here
    // would contradict the row's own thumb and quietly re-mark the replaced
    // cover as the one in use.
    artCurrentUrl = t.coverart_url || null;
    overlayImg.hidden = !t.coverart_url;
    if (t.coverart_url) overlayImg.src = artSrc(t.coverart_url);
    setPickNote(t.coverart_url);
  } else {
    artCurrentUrl = null;
    overlayImg.hidden = false;
    overlayImg.src = "/api/artwork?index=" + t.index + "&v=" + S.artVersion;
    artNote.hidden = true;
  }
  const i = S.tracks.indexOf(t);
  document.getElementById("art-title").textContent = trackName(t);
  document.getElementById("art-sub").textContent = (i < 0 ? "" : "Track " + (i + 1) + " · ")
    + TL.fmt(startOf(t)) + (t.rejected ? " · merged, so not in the chapters" : "");
  document.getElementById("art-prev").disabled = !stepTarget(-1);
  document.getElementById("art-next").disabled = !stepTarget(1);
  syncCoverButton();
  // The other covers load with the dialog now, rather than behind a button:
  // the server caches each track's answer, so a reopened track costs nothing.
  loadAlternates(t);
  if (!artDialog.open) artDialog.showModal();
}

// ⌘[ / ⌘] and the ‹ › buttons: the previous or next track with artwork,
// without closing, so a whole set can be curated in one pass. The selection
// follows, so the list and inspector stay on the track being looked at.
function stepTarget(d) {
  const i = S.tracks.indexOf(artTrack);
  if (i < 0) return null;
  for (let j = i + d; j >= 0 && j < S.tracks.length; j += d) if (walkable(S.tracks[j])) return S.tracks[j];
  return null;
}
export function stepArtwork(d) {
  const t = stepTarget(d);
  if (!t) return;
  artProbeSeq++;  // a paste still loading belongs to the track being left
  selectTrack(t, { keepTab: true });
  showArtwork(t);
  // Stepping rebuilds the grid, and a focused tile goes with it.
  keepFocus(artDialog, document.getElementById(d < 0 ? "art-prev" : "art-next"));
}
document.getElementById("art-prev").onclick = () => stepArtwork(-1);
document.getElementById("art-next").onclick = () => stepArtwork(1);

export function closeArtwork() {
  if (artDialog.open) artDialog.close();
}
artDialog.addEventListener("close", () => {
  overlayImg.src = "";
  artGrid.replaceChildren();
  artTrack = null;
  artProbeSeq++;
});

// The dialog's own keys. The page's keyboard map stands down while a dialog
// is open (see keys.js), so these are the only shortcuts that apply here.
artDialog.addEventListener("keydown", e => {
  const cmd = (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey;
  if (cmd && (e.key === "[" || e.key === "]")) {
    e.preventDefault();
    stepArtwork(e.key === "[" ? -1 : 1);
    return;
  }
  // Arrows walk the candidate grid once a tile has focus, or from the dialog
  // itself; Enter on a tile is the button's own click.
  const tiles = [...artGrid.querySelectorAll(".alt")];
  if (!tiles.length || e.altKey || e.metaKey || e.ctrlKey) return;
  const at = tiles.indexOf(document.activeElement);
  const inField = document.activeElement && document.activeElement.tagName === "INPUT";
  if (inField || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
  e.preventDefault();
  if (at < 0) { (tiles.find(x => x.classList.contains("current")) || tiles[0]).focus(); return; }
  // Columns from the layout, so it follows the grid's auto-fill at any width.
  const cols = Math.max(1, tiles.filter(x => x.offsetTop === tiles[0].offsetTop).length);
  const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols }[e.key];
  const next = tiles[at + step];
  if (next) next.focus();
});

export function syncCoverButton() {
  const on = !!(artTrack && artTrack.episode_cover);
  const rejected = !!(artTrack && artTrack.rejected);
  artCoverBtn.textContent = (on ? "★" : "☆") + " Episode cover";
  artCoverBtn.classList.toggle("on", on);
  // A rejected track is absent from the output, and from the sidecar the star
  // is stored in, so it cannot be the cover. The server refuses the star for
  // one; this stops the page offering a choice that would silently evaporate.
  artCoverBtn.disabled = rejected;
  artCoverBtn.title = rejected
    ? "A rejected track cannot be the episode cover"
    : on
      ? "This track's artwork is the episode cover"
      : "Use this track's artwork as the episode cover";
}

// Exactly one track can be the episode cover, so starring one clears the
// rest. The server re-applies the same rule on save, so a stale page cannot
// leave two starred in the sidecar.
artCoverBtn.onclick = () => {
  if (!artTrack || artTrack.rejected) return;
  const on = !artTrack.episode_cover;
  S.tracks.forEach(t => { t.episode_cover = false; });
  artTrack.episode_cover = on;
  // One episode cover: starring a track replaces an uploaded one.
  if (on && hasUploadedCover()) S.coverChoice = null;
  setDirty(true);
  syncCoverButton();
  render();
};

// Lazy and per-track: the server asks every source here instead of stopping
// at the first that answers, so this costs a handful of third-party requests
// and must never run for a track the user did not open.
export async function loadAlternates(t) {
  artGrid.innerHTML = '<div class="status">Searching iTunes, Deezer and Cover Art Archive…</div>';
  let data;
  try {
    // The live artist/title, not the saved ones: correcting a bad
    // identification and then fixing its cover is one workflow, and searching
    // the stale name would offer covers for the song being corrected away.
    const query = "/api/artwork/options?index=" + t.index
      + "&artist=" + encodeURIComponent(t.artist || "")
      + "&title=" + encodeURIComponent(t.title || "");
    data = await (await fetch(query)).json();
  } catch (e) {
    artGrid.innerHTML = '<div class="status">Could not reach the artwork sources.</div>';
    return;
  }
  if (t !== artTrack) return;  // the panel moved to another track while we waited
  const found = (data && data.candidates) || [];
  const failed = !!(data && data.error);
  if (!found.length) {
    // "Nothing found" and "the lookup blew up" are different answers, and the
    // second one is worth retrying.
    artGrid.innerHTML = failed
      ? '<div class="status">Could not reach the artwork sources.</div>'
      : '<div class="status">No alternates found for this track.</div>';
    return;
  }
  artGrid.innerHTML = "";
  // The server's flag reflects *saved* state; an unsaved pick made on this
  // page is the better answer and keeps its marker.
  if (!t._art) {
    const inUse = found.find(c => c.current);
    artCurrentUrl = inUse ? inUse.url : null;
  }
  for (const c of found) {
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "alt";
    tile.dataset.url = c.url;
    tile.title = c.label ? c.source + " — " + c.label : c.source;
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = tile.title;
    // Loaded straight from the source, like the player bar's thumbnail: the
    // covers differ only in the picture, and the identical lower-third they
    // would all be composited under adds nothing to the comparison.
    img.src = artSrc(c.url);
    const cap = document.createElement("span");
    cap.textContent = c.source;  // textContent, not innerHTML — no XSS surface
    tile.append(img, cap);
    tile.onclick = () => chooseArtwork(c.url);
    artGrid.append(tile);
  }
  if (failed) {
    // Some tiles arrived, but not all of them: say so rather than let a short
    // grid read as "this is everything there is".
    const status = document.createElement("div");
    status.className = "status";
    status.textContent = "Some artwork sources could not be reached.";
    artGrid.append(status);
  }
  markCurrentAlt();
  // Focus that was on the grid's "Searching…" (or on a tile of the track just
  // left) went with it: land on the cover in use, ready for the arrows.
  keepFocus(artDialog, artGrid.querySelector(".alt.current") || artGrid.querySelector(".alt"));
}

export function markCurrentAlt() {
  artGrid.querySelectorAll(".alt").forEach(
    el => el.classList.toggle("current", !!artCurrentUrl && el.dataset.url === artCurrentUrl));
}

// A pick lands in coverart_url, which is what the artwork cache keys on and
// what the embed reads. `_art` marks the row as carrying a deliberate choice:
// it is why the save payload includes the URL at all, and why the thumb
// previews the raw image until the server has rebuilt the composite.
export function chooseArtwork(url) {
  if (!artTrack) return;
  artTrack.coverart_url = url || null;
  artTrack._art = true;
  artCurrentUrl = url || null;
  setDirty(true);
  overlayImg.hidden = !url;
  if (url) overlayImg.src = artSrc(url);
  setPickNote(url);
  markCurrentAlt();
  render();
}

document.getElementById("art-auto").onclick = () => chooseArtwork(null);

// ---- uploads ----
// An uploaded image is stored beside the tracklist straight away and comes
// back as an "upload:<hash>.jpg" reference, which is then *chosen* like any
// other cover: an ordinary edit, undoable, written on Save. The body is the
// file itself with its image/* type -- the server refuses anything else, so
// a cross-site form cannot post one.
export function artSrc(url) {
  return url && url.startsWith("upload:") ? "/api/upload/" + url.slice(7) : url;
}

// ---- one picture per track (#60) ----
// Every thumbnail -- the list, the timeline, the inspector, the player bar --
// takes its background from here, so a track shows the same art everywhere.
// It is the cover as found (/api/artwork/source), not the chapter composite:
// the lower-third is unreadable at thumbnail size and belongs to the artwork
// panel, which previews what Embed writes. Two layers, the cover over the
// track's gradient: a cover still loading, or a 404 (no art anywhere), paints
// nothing and the gradient shows through, so there is no load state to keep.
// Keyed by saved state like the composite, so S.artVersion (bumped by every
// save and load) is what makes it fetch again. `near` false gives just the
// gradient, for a list row not yet scrolled near the viewport.
export function artFor(t, i, near = true) {
  const grad = GRADS[i % GRADS.length];
  if (t._art) {
    // Picked in this session and not saved yet: the server still has the old
    // art, so show the raw choice. "Automatic" has no URL until the save.
    return t.coverart_url ? cssUrl(artSrc(t.coverart_url)) + ", " + grad : grad;
  }
  // 0 is a real index; an unsaved inserted row has none.
  const saved = t.index !== undefined && t.index !== null;
  if (!near || !saved || !(t.artist || t.title)) return grad;
  return cssUrl("/api/artwork/source?index=" + t.index + "&v=" + S.artVersion) + ", " + grad;
}

function cssUrl(u) {
  return "url('" + String(u).replace(/'/g, "%27") + "')";
}

export async function uploadImage(file) {
  if (!file || !(file.type || "").startsWith("image/")) throw new Error("That file is not an image.");
  if (file.size > 25 * 1024 * 1024) throw new Error("That image is over 25 MB.");
  const r = await fetch("/api/upload", { method: "POST", headers: { "Content-Type": file.type }, body: file });
  const res = await r.json().catch(() => ({}));
  if (!r.ok || !res.ok) throw new Error(res.error ? "Upload failed: " + res.error : "Upload failed (" + r.status + ")");
  return res.ref;
}

// A file dropped anywhere else would make the browser open it in place of the
// editor; outside the two drop zones a drop does nothing.
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => e.preventDefault());

// Drop targets: stop the browser opening the file, show where it will land.
export function dropZone(node, onFile) {
  node.addEventListener("dragover", e => { e.preventDefault(); node.classList.add("drop"); });
  node.addEventListener("dragleave", () => node.classList.remove("drop"));
  node.addEventListener("drop", e => {
    e.preventDefault();
    node.classList.remove("drop");
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) onFile(file);
  });
}

export async function uploadTrackArt(file) {
  const target = artTrack;  // the panel may move on while the upload runs
  artNote.textContent = "Uploading…";
  artNote.hidden = false;
  try {
    const ref = await uploadImage(file);
    if (target && target === artTrack) chooseArtwork(ref);
  } catch (e) {
    if (target === artTrack) artNote.textContent = e.message;
  }
}
export const artFile = document.getElementById("art-file");
document.getElementById("art-upload").onclick = () => artFile.click();
artFile.onchange = () => { if (artFile.files[0]) uploadTrackArt(artFile.files[0]); artFile.value = ""; };
dropZone(artDialog, file => { if (!S.liveMode) uploadTrackArt(file); });

// ---- episode cover ----
// The file <set>_cover.jpg beside the tracklist, embedded as the MP3's cover
// by Embed chapters. S.coverVersion / S.coverChoice: see state.js.
// The MP3's own title (also written as its album) and artist.
for (const el of [epTitle, epArtist]) {
  el.addEventListener("input", () => setDirty(true));
  el.addEventListener("keydown", e => { if (e.key === "Enter") el.blur(); });
}

export function hasUploadedCover() {
  return typeof S.coverChoice === "string" || (S.coverChoice === undefined && S.coverVersion !== null);
}

export function renderCover() {
  const src = typeof S.coverChoice === "string" ? artSrc(S.coverChoice)
    : S.coverChoice === undefined && S.coverVersion !== null ? "/api/cover?v=" + S.coverVersion : null;
  const thumb = document.getElementById("ep-thumb");
  thumb.style.backgroundImage = src ? "url('" + src + "')" : "";
  thumb.classList.toggle("empty", !src);
  document.getElementById("ep-remove").hidden = !src;
  document.getElementById("embed").hidden = !S.canEmbed;
  const star = S.tracks.find(t => t.episode_cover && !t.rejected);
  document.getElementById("ep-note").textContent = src
    ? (typeof S.coverChoice === "string" ? "Cover uploaded — Save to keep it" : "Cover uploaded")
    : star ? "Cover from ★ " + trackName(star) : "Cover: the first track with artwork";
}

export async function uploadCover(file) {
  if (S.liveMode) return;
  document.getElementById("ep-note").textContent = "Uploading…";
  try {
    S.coverChoice = await uploadImage(file);
    S.tracks.forEach(t => { t.episode_cover = false; });  // one episode cover
    setDirty(true);
    render();
    toast("Episode cover set — Save to keep it");
  } catch (e) {
    renderCover();
    toast(e.message);
  }
}
export const epFile = document.getElementById("ep-file");
document.getElementById("ep-upload").onclick = () => epFile.click();
epFile.onchange = () => { if (epFile.files[0]) uploadCover(epFile.files[0]); epFile.value = ""; };
dropZone(document.getElementById("episode"), uploadCover);
document.getElementById("ep-remove").onclick = () => {
  // Removing an upload that was never saved is back to "no change".
  S.coverChoice = S.coverVersion === null ? undefined : null;
  setDirty(true);
  render();
};

// Everything `setlist-maker chapters` does, from the saved tracklist -- so an
// unsaved page is saved first. The MP3 is rewritten in place while the
// player may be streaming it, so playback stops and reloads around it.
export let embedding = false;
document.getElementById("embed").onclick = async () => {
  if (S.liveMode || embedding) return;
  if (S.dirty && !(await save())) return;
  const btn = document.getElementById("embed");
  embedding = true;
  btn.disabled = true;
  btn.textContent = "Embedding…";
  const at = audio.currentTime || 0, wasPlaying = !audio.paused;
  audio.pause();
  try {
    const r = await fetch("/api/chapters", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const res = await r.json().catch(() => ({}));
    if (res.ok) {
      toast("Embedded " + res.chapters + " chapters, " + res.images + " images"
        + (res.cover ? " and the episode cover" : "") + " into the MP3");
    } else {
      toast("Embed failed: " + (res.error || r.status));
    }
  } catch (e) {
    toast("Embed failed");
  } finally {
    embedding = false;
    btn.disabled = false;
    btn.textContent = "Embed chapters in MP3";
    audio.src = "/api/audio?v=" + Date.now();
    audio.addEventListener("loadedmetadata", () => {
      audio.currentTime = at;
      if (wasPlaying) audio.play();
    }, { once: true });
  }
};
document.getElementById("art-url-use").onclick = () => useTypedUrl();
artUrlEl.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); useTypedUrl(); }
});

// Load the URL here before pinning it. The server falls through its waterfall
// when a cover URL fails to download, so a dead link would not error -- it
// would quietly composite some other source's art under this track's name.
export function useTypedUrl() {
  const url = artUrlEl.value.trim();
  if (!url) return;
  if (!/^https?:\/\//i.test(url)) {
    artNote.textContent = "Artwork URLs must start with http:// or https://.";
    artNote.hidden = false;
    return;
  }
  artNote.textContent = "Loading that image…";
  artNote.hidden = false;
  // Captured, like loadAlternates does: the probe resolves asynchronously, so
  // pinning to whatever artTrack happens to be by then would land the URL on a
  // different track (or on none, if the panel was closed meanwhile). The
  // sequence number covers the other direction -- two pastes for the *same*
  // track, where a slower first probe would otherwise land last and win.
  const target = artTrack;
  const seq = ++artProbeSeq;
  const live = () => seq === artProbeSeq && target === artTrack;
  const probe = new Image();
  probe.onload = () => { if (live()) chooseArtwork(url); };
  probe.onerror = () => {
    if (live()) artNote.textContent = "That URL did not load as an image.";
  };
  probe.src = url;
}
