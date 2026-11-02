// Row thumbnails, the artwork panel and picker, uploads, the episode cover, Embed.

import { S } from "./state.js";
import { audio, epArtist, epTitle, toast } from "./dom.js";
import { trackName } from "./util.js";
import { setDirty } from "./history.js";
import { render, save } from "./main.js";

// Generate visible rows first: 60-track sets are network-bound on a miss.
export const artObserver = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const el = e.target;
    artObserver.unobserve(el);
    const img = new Image();
    // 404 = unidentified or out of range; keep the gradient placeholder.
    img.onload = () => { el.style.backgroundImage = "url('" + img.src + "')"; };
    img.src = "/api/artwork?index=" + el.dataset.index + "&v=" + S.artVersion;
  }
}, { rootMargin: "200px" });  // start just before the row scrolls into view

export const overlay = document.getElementById("artwork-overlay");
export const overlayImg = document.getElementById("artwork-large");
export const artNote = document.getElementById("artwork-note");
export const artAlts = document.getElementById("artwork-alts");
export const artGrid = document.getElementById("artwork-grid");
export const artUrlEl = document.getElementById("art-url");
export const artCoverBtn = document.getElementById("art-cover");

// The track the panel is showing, held as an object reference rather than a
// position: render() rebuilds every row and addBelow()/commit() can re-sort
// the array while the panel is open, so a stored index would go stale the
// same way playingIndex does (hence reindexPlayingTrack).
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

export function showArtwork(t) {
  artTrack = t;
  artAlts.hidden = true;
  artGrid.innerHTML = "";
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
  syncCoverButton();
  overlay.classList.add("open");
}

export function closeArtwork() {
  overlay.classList.remove("open");
  overlayImg.src = "";
  artTrack = null;
}

// Only the backdrop dismisses; the panel is full of controls now. Escape
// still routes through here, since overlay.click() targets the overlay itself.
overlay.onclick = (e) => { if (e.target === overlay) closeArtwork(); };

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

document.getElementById("art-choose").onclick = () => {
  if (!artTrack) return;
  artAlts.hidden = !artAlts.hidden;
  if (!artAlts.hidden && !artGrid.childElementCount) loadAlternates(artTrack);
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
dropZone(document.getElementById("artwork-panel"), file => { if (!S.liveMode) uploadTrackArt(file); });

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
