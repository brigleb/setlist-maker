// The selected track's inspector, and asking Shazam about a moment.

import { GRADS, S, TL } from "./state.js";
import { audio, el, toast } from "./dom.js";
import { trackName } from "./util.js";
import { setDuration } from "./model.js";
import { addBelow } from "./list.js";
import { playAt, playFrom } from "./player.js";
import { pendingSamples, renderTimeline } from "./grid.js";
import { setNames, splitAtPlayhead, toggleMerge } from "./edits.js";
import { artSrc, showArtwork } from "./artwork.js";
import { render } from "./main.js";

export function renderInspector() {
  const box = document.getElementById("insp");
  box.innerHTML = "";
  const t = S.selectedTrack;
  const i = t ? S.tracks.indexOf(t) : -1;
  if (i === -1) {
    S.selectedTrack = null;
    box.appendChild(el("h3", null, "Track"));
    box.appendChild(el("div", "empty", "Click a track on the timeline — or anything in “Needs a look” — to see what Shazam heard there and fix it."));
    return;
  }
  const D = setDuration();
  // A kept track owns everything merged into it, so its span and its samples
  // are the kept window's; a merged row shows its own old span.
  const kept = TL.keptWindows(S.tracks, D);
  const k = kept.findIndex(x => x.index === i);
  const w = k === -1 ? TL.windows(S.tracks, D)[i] : kept[k];
  const joined = TL.joinProbes(S.tracks, S.probes, D)[i];

  const hero = el("div", "in-hero");
  const thumb = el("div", "thumb");
  thumb.style.backgroundImage = t.coverart_url
    ? "url('" + artSrc(t.coverart_url).replace(/'/g, "%27") + "')" : GRADS[i % GRADS.length];
  const canArt = (t.artist || t.title) && t.index !== undefined && t.index !== null;
  if (canArt) { thumb.title = "Artwork and episode cover"; thumb.onclick = () => showArtwork(t); }
  else thumb.style.cursor = "default";
  hero.appendChild(thumb);
  const who = el("div");
  who.style.minWidth = "0";
  who.appendChild(el("div", "in-when", TL.fmt(w.start) + " – " + TL.fmt(w.end) + " · " + TL.fmt(w.end - w.start)));
  who.appendChild(el("div", "in-when", "track " + (i + 1) + " of " + S.tracks.length));
  const badges = el("div", "in-badges");
  if (t.rejected) badges.appendChild(el("span", "badge warn", "merged into previous"));
  if (t._new) badges.appendChild(el("span", "badge", "new — unsaved"));
  if (t.episode_cover) badges.appendChild(el("span", "badge", "★ episode cover"));
  badges.appendChild(el("span", "badge", joined.length + " sample" + (joined.length === 1 ? "" : "s")));
  who.appendChild(badges);
  hero.appendChild(who);
  box.appendChild(hero);

  const artistIn = el("input", "in-artist");
  artistIn.value = t.artist; artistIn.placeholder = "Artist"; artistIn.setAttribute("aria-label", "Artist");
  const titleIn = el("input", "in-title");
  titleIn.value = t.title; titleIn.placeholder = "Title"; titleIn.setAttribute("aria-label", "Title");
  const commit = () => { if (setNames(t, artistIn.value, titleIn.value)) render(); };
  artistIn.readOnly = titleIn.readOnly = S.liveMode;
  for (const input of [artistIn, titleIn]) {
    input.addEventListener("change", commit);
    input.addEventListener("keydown", ev => {
      if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
      if (ev.key === "Escape") { input.value = input === artistIn ? t.artist : t.title; input.blur(); }
    });
  }
  box.appendChild(el("div", "in-lbl", "Artist"));
  box.appendChild(artistIn);
  box.appendChild(el("div", "in-lbl", "Title"));
  box.appendChild(titleIn);

  // Every name Shazam gave the audio in this window. The neighbours' names are
  // left out: a probe at the edge hearing the next track is not a candidate.
  const neighbours = k === -1 ? [] : [kept[k - 1], kept[k + 1]].filter(Boolean).map(x => S.tracks[x.index]);
  const names = TL.variants(joined, neighbours);
  if (names.length) {
    box.appendChild(el("div", "in-lbl", "Shazam heard"));
    for (const v of names) {
      const line = el("div", "in-variant");
      line.appendChild(el("div", "nm", v.artist + " — " + v.title)).title = v.artist + " — " + v.title;
      line.appendChild(el("span", "ct", "×" + v.count));
      const current = v.artist === t.artist && v.title === t.title;
      const use = el("button", "tbtn", current ? "in use" : "Use");
      use.disabled = current || S.liveMode;
      use.onclick = () => { if (setNames(t, v.artist, v.title)) render(); };
      line.appendChild(use);
      box.appendChild(line);
    }
  }

  if (joined.length) {
    box.appendChild(el("div", "in-lbl", "Samples — click to hear one"));
    const list = el("div", "in-samples");
    for (const p of joined) {
      const b = el("button");
      b.type = "button";
      b.appendChild(el("span", null, TL.fmt(p.t)));
      const hit = p.artist || p.title;
      b.appendChild(el("span", "nm" + (hit ? "" : " miss"), hit ? (p.artist || "?") + " — " + (p.title || "?") : "no match"));
      b.appendChild(el("span", null, p.purpose === "refine" ? "12s" : p.window + "s"));
      b.title = p.purpose + (p.confidence ? " · confidence " + p.confidence : "");
      b.onclick = () => playAt(p.t);
      list.appendChild(b);
    }
    box.appendChild(list);
  }

  // What was merged into this track, one Unmerge each: the grid shows these
  // only as seams too close together to click.
  if (k !== -1) {
    const end = kept[k + 1] ? kept[k + 1].index : S.tracks.length;
    const folded = S.tracks.slice(i + 1, end);
    if (folded.length) {
      box.appendChild(el("div", "in-lbl", "Merged into this track"));
      for (const m of folded) {
        const line = el("div", "in-variant");
        line.appendChild(el("div", "nm", TL.fmt(m.timestamp) + " " + trackName(m))).title = trackName(m);
        const un = el("button", "tbtn", "Unmerge");
        un.disabled = S.liveMode;
        un.onclick = () => toggleMerge(m);
        line.appendChild(un);
        box.appendChild(line);
      }
    }
  }

  const btns = el("div", "in-btns");
  const play = el("button", "tbtn primary", "▶ Play");
  play.onclick = () => playFrom(i, { scroll: false });
  const split = el("button", "tbtn in-split", "Split at playhead");
  const now = audio.currentTime || 0;
  split.disabled = !(now > w.start + 1 && now < w.end - 1);
  split.title = split.disabled ? "Play inside this track, then split where the next one starts" : "Start a new track at " + TL.fmt(now);
  split.onclick = splitAtPlayhead;
  const ident = el("button", "tbtn in-identify", "Identify at playhead");
  ident.disabled = !S.canSample || !(now >= w.start && now < w.end);
  ident.title = !S.canSample ? "Sampling needs the set's audio"
    : ident.disabled ? "Play inside this track, then ask Shazam what is playing (I)"
    : "Ask Shazam what is playing at " + TL.fmt(now) + " and name this track (I)";
  ident.onclick = () => identifyAt(t, audio.currentTime || 0);
  ident.hidden = !!t.rejected;  // a merged row is not in the output to be named
  const merge = el("button", "tbtn", t.rejected ? "Unmerge" : (i === 0 ? "Reject" : "Merge into previous"));
  merge.onclick = () => toggleMerge(t);
  const add = el("button", "tbtn", "＋ Add below");
  add.onclick = () => addBelow(i);
  btns.append(play);
  if (!S.liveMode) btns.append(ident, split, merge, add);
  if (canArt && !S.liveMode) {
    const artBtn = el("button", "tbtn", "Artwork…");
    artBtn.onclick = () => showArtwork(t);
    btns.appendChild(artBtn);
  }
  box.appendChild(btns);
  if (t.rejected) box.appendChild(el("div", "in-note", "Merged tracks are left out of the tracklist when you save; their time goes to the track before."));
  if (S.liveMode) box.appendChild(el("div", "in-note", "Still identifying — this is the run's current answer, and it may change as the run narrows the boundaries. Editing opens when it finishes."));
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
