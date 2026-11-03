// Every action the page offers, in one place (#59): the context menus and the
// ⌘K palette both read COMMANDS, and each command calls the same function its
// button, gesture or key already does, so the three cannot drift apart.
//
// A command is { id, group, label, menu?, keys?, playhead?, when, run }.
//   label   what the palette says (no context: "the selected track")
//   menu    what a context menu says, a string or a function of the context
//   keys    the shortcut, shown beside it in both
//   playhead  in the palette, act on the track under the playhead rather than
//           the selected one, as S and I do
//   when(c) true: offer it; false: leave it out; a string: grey it out, and
//           that string is why
//   run(c)  do it
// The context `c` is { track, at, pointer, probe }: the track the menu is for,
// the moment (the pointer's, or the playhead's), whether that moment is the
// pointer's, and the sample a pin menu is for.
//
// Top-level code here only declares closures and registers listeners, never
// calls into another module: the modules import each other in cycles (see
// CLAUDE.md).

import { S, TL } from "./state.js";
import { audio, playerEl, pplayEl, toast } from "./dom.js";
import { trackName } from "./util.js";
import { keptTrackAt, setDuration, splitWindow } from "./model.js";
import { redo, undo } from "./history.js";
import { nextTrack, playAt, playFrom, playSelected, prevTrack, skip, toggleFollow } from "./player.js";
import { mergeBlocked, mergeInto, moveStart, setNames, splitAt, startOf } from "./edits.js";
import { identifyAt, sampleAt } from "./inspector.js";
import { hasArtwork, setEpisodeCover, showArtwork } from "./artwork.js";
import { selectTrack, timeAt, zoomAll, zoomBy, zoomToTrack } from "./grid.js";
import { addBelow, focusSelected, startEdit } from "./list.js";
import { render, save } from "./main.js";
import { showTab } from "./shell.js";
import { openSheet } from "./keys.js";
import { dialogOpen } from "./dialog.js";
import { menuJustOpened, openMenu } from "./menu.js";
import { keyOf } from "./ui.js";

const LIVE = "Editing opens when the run finishes";
const NO_AUDIO = "The audio is still loading";
const NO_SAMPLER = "Sampling needs the set's audio";
const NO_TRACK = "Select a track first";
const audioReady = () => !playerEl.classList.contains("disabled");
const now = () => audio.currentTime || 0;
const indexOf = t => S.tracks.indexOf(t);
const kept = () => S.tracks.filter(x => !x.rejected);
const edit = c => (!c.track ? NO_TRACK : S.liveMode ? LIVE : true);
// A toolbar or panel button's own click, so its handler stays the one place
// that action lives. Left out while the button is hidden.
const button = id => document.getElementById(id);
const shown = id => !!button(id) && !button(id).hidden;

async function copyTime(seconds) {
  const text = TL.fmt(seconds);
  try { await navigator.clipboard.writeText(text); toast("Copied " + text); }
  catch (e) { toast("Couldn't copy " + text); }
}

// Where a split at `at` would land, as splitAt() decides it.
function splitWhy(c) {
  const t = c.track;
  if (!t) return S.liveMode ? LIVE : "Play inside a track first";
  if (S.liveMode) return LIVE;
  const w = splitWindow(t), at = Math.floor(c.at);
  if (w && at > w.start && at < w.end && at > t.timestamp) return true;
  if (!c.pointer) return "Play inside this track first";
  return w && at > w.start && at <= t.timestamp
    ? "That part was merged in from the track before" : "Too close to the edge of the track";
}

function identifyWhy(c) {
  const t = c.track;
  if (t && t.rejected) return false;
  if (!S.canSample) return NO_SAMPLER;
  if (S.liveMode) return LIVE;
  if (!t) return "Play inside a track first";
  const w = splitWindow(t);
  return w && c.at >= w.start && c.at < w.end ? true : "Play inside this track first";
}

export const COMMANDS = [
  // ---- the track ----
  { id: "play-start", group: "Track", label: "Play the selected track from its start", menu: "Play from its start",
    keys: ["⇧Space"], when: c => (!c.track ? NO_TRACK : audioReady() || NO_AUDIO),
    run: c => (c.track === S.selectedTrack ? playSelected() : playFrom(indexOf(c.track), { scroll: false })) },
  { id: "play-here", group: "Track", label: "Play from here", menu: c => "Play from " + TL.fmt(c.at),
    when: c => (c.pointer ? audioReady() || NO_AUDIO : false), run: c => playAt(c.at) },
  { id: "split", group: "Track", label: "Split at the playhead", playhead: true, keys: ["S"],
    menu: c => (c.pointer ? "Split here at " + TL.fmt(c.at) : "Split at the playhead"),
    when: splitWhy, run: c => { if (splitAt(c.track, c.at)) focusSelected(); } },
  { id: "identify", group: "Track", label: "Identify at the playhead (asks Shazam)", playhead: true, keys: ["I"],
    menu: c => (c.pointer ? "Identify here (asks Shazam)" : "Identify at the playhead"),
    when: identifyWhy, run: c => identifyAt(c.track, c.at) },
  { id: "start-here", group: "Track", label: "Start the selected track at the playhead", menu: "Start it at the playhead",
    when: c => (c.track && c.track.rejected ? false : c.pointer ? false : edit(c) !== true ? edit(c) : audioReady() || NO_AUDIO),
    run: c => { if (!moveStart(c.track, now())) toast("It can't start there: it would pass the track next to it"); } },
  { id: "rename", group: "Track", label: "Rename the selected track", menu: "Rename", keys: ["Enter"],
    when: edit, run: c => { selectTrack(c.track); startEdit(c.track, "artist", { select: true }); } },
  { id: "insert", group: "Track", label: "Insert a track below the selected one", menu: "Insert a track below",
    when: edit, run: c => addBelow(indexOf(c.track)) },
  { id: "merge-prev", group: "Track", label: "Merge the selected track into the previous one", keys: ["⌫"],
    menu: () => (kept().length === 1 ? "Reject" : "Merge ← into the previous track"),
    when: c => (c.track && c.track.rejected ? false : edit(c) !== true ? edit(c) : mergeBlocked(c.track, "prev") || true),
    run: c => mergeInto(c.track, "prev") },
  { id: "merge-next", group: "Track", label: "Merge the selected track into the next one", keys: ["⇧⌫"],
    menu: "Merge → into the next track",
    when: c => (!c.track ? NO_TRACK : c.track.rejected || kept().length === 1 ? false
      : S.liveMode ? LIVE : mergeBlocked(c.track, "next") || true),
    run: c => mergeInto(c.track, "next") },
  { id: "restore", group: "Track", label: "Bring the selected track back", keys: ["⌫"],
    menu: c => (TL.owners(S.tracks, setDuration())[indexOf(c.track)] === -1 ? "Bring it back" : "Unmerge"),
    when: c => (c.track && c.track.rejected ? (S.liveMode ? LIVE : true) : false),
    run: c => mergeInto(c.track, "prev") },
  { id: "artwork", group: "Track", label: "Artwork for the selected track…", menu: "Artwork…",
    when: c => (!c.track ? NO_TRACK : !hasArtwork(c.track) ? false : S.liveMode ? LIVE : true),
    run: c => showArtwork(c.track) },
  { id: "cover", group: "Track",
    label: c => (c.track && c.track.episode_cover ? "Stop using the selected track's artwork as the episode cover"
      : "Use the selected track's artwork as the episode cover"),
    menu: c => (c.track.episode_cover ? "★ Stop using as the episode cover" : "☆ Use as the episode cover"),
    when: c => (!c.track ? NO_TRACK : !hasArtwork(c.track) ? false : S.liveMode ? LIVE
      : c.track.rejected ? "A merged track can't be the episode cover" : true),
    run: c => setEpisodeCover(c.track, !c.track.episode_cover) },
  { id: "zoom-track", group: "Track", label: "Zoom to the selected track", menu: "Zoom to it", keys: ["Z"],
    when: c => (!c.track ? NO_TRACK : true), run: c => zoomToTrack(c.track) },
  { id: "copy-time", group: "Track", label: "Copy the playhead's time",
    menu: c => "Copy " + TL.fmt(c.copyAt ?? c.at), when: () => true, run: c => copyTime(c.copyAt ?? c.at) },

  // ---- a sample (pin menus only) ----
  { id: "play-sample", group: "Sample", label: "Play this sample", menu: "Play what Shazam heard",
    when: c => (c.probe ? audioReady() || NO_AUDIO : false), run: c => playAt(c.probe.t) },
  { id: "use-name", group: "Sample", label: "Use this name",
    menu: c => "Use this name for " + trackName(c.track),
    when: c => {
      const p = c.probe;
      if (!p || !(p.artist || p.title) || !c.track) return false;
      if (S.liveMode) return LIVE;
      return TL.identity(p.artist, p.title) === TL.identity(c.track.artist, c.track.title) ? "It's already this track's name" : true;
    },
    run: c => { if (setNames(c.track, c.probe.artist || "", c.probe.title || "")) render(); } },
  { id: "ask", group: "Sample", label: "Ask Shazam about the playhead",
    menu: c => "Ask Shazam about " + TL.fmt(c.at),
    when: () => S.canSample || NO_SAMPLER, run: c => sampleAt(Math.round(c.at)) },

  // ---- playback ----
  { id: "play-pause", group: "Playback", label: "Play / pause", keys: ["Space"],
    when: () => audioReady() || NO_AUDIO, run: () => pplayEl.click() },
  { id: "back", group: "Playback", label: "Back 5 seconds", keys: ["←"], when: () => audioReady() || NO_AUDIO, run: () => skip(-5) },
  { id: "forward", group: "Playback", label: "Forward 5 seconds", keys: ["→"], when: () => audioReady() || NO_AUDIO, run: () => skip(5) },
  { id: "prev-track", group: "Playback", label: "Play the previous track", keys: ["⌥↑"], when: () => audioReady() || NO_AUDIO, run: () => prevTrack() },
  { id: "next-track", group: "Playback", label: "Play the next track", keys: ["⌥↓"], when: () => audioReady() || NO_AUDIO, run: () => nextTrack() },
  { id: "follow", group: "Playback",
    label: () => (S.follow ? "Stop following the playhead" : "Follow the playhead: the selection moves with the audio"),
    keys: ["F"], when: () => true, run: () => toggleFollow() },

  // ---- the timeline ----
  { id: "zoom-all", group: "Timeline", label: "Show the whole set", keys: ["0"], when: () => true, run: () => zoomAll() },
  { id: "zoom-in", group: "Timeline", label: "Zoom in", keys: ["="], when: () => true, run: () => zoomBy(0.5) },
  { id: "zoom-out", group: "Timeline", label: "Zoom out", keys: ["-"], when: () => true, run: () => zoomBy(2) },
  { id: "replay", group: "Timeline", label: "Replay the run", when: () => S.probes.length > 0, run: () => button("tl-replay").click() },

  // ---- the set ----
  { id: "save", group: "Set", label: "Save", keys: ["⌘S"],
    when: () => (S.liveMode ? LIVE : S.dirty || "Nothing to save"), run: () => save() },
  { id: "undo", group: "Set", label: "Undo", keys: ["⌘Z"], when: () => !button("undo").disabled || "Nothing to undo", run: () => undo() },
  { id: "redo", group: "Set", label: "Redo", keys: ["⇧⌘Z"], when: () => !button("redo").disabled || "Nothing to redo", run: () => redo() },
  { id: "set-tab", group: "Set", label: "Edit the set's description and episode", when: () => true, run: () => showTab("set") },
  { id: "queue-tab", group: "Set", label: "Show what needs a look", when: () => true, run: () => showTab("queue") },
  { id: "upload-cover", group: "Set", label: "Upload an episode cover…", when: () => (S.liveMode ? LIVE : true),
    run: () => { showTab("set"); button("ep-upload").click(); } },
  { id: "remove-cover", group: "Set", label: "Remove the uploaded episode cover",
    when: () => (!shown("ep-remove") ? false : S.liveMode ? LIVE : true), run: () => button("ep-remove").click() },
  { id: "embed", group: "Set", label: "Embed chapters in the MP3", when: () => shown("embed"),
    run: () => { showTab("set"); button("embed").click(); } },
  { id: "shortcuts", group: "Set", label: "Show the keyboard shortcuts", keys: ["?"], when: () => true, run: () => openSheet() },
  { id: "done", group: "Set", label: "Done: close the editor", when: () => true, run: () => button("done").click() },
];

const byId = id => COMMANDS.find(x => x.id === id);
const text = (v, c) => (typeof v === "function" ? v(c) : v);

// A command as one context sees it: its words, keys and state.
export function resolve(cmd, c, where) {
  const state = cmd.when(c);
  if (state === false) return null;
  const words = where === "menu" && cmd.menu ? text(cmd.menu, c) : text(cmd.label, c);
  // S and I act on the playhead, so their keys don't apply to a moment the
  // pointer picked.
  const keys = cmd.keys && !(where === "menu" && c.pointer && cmd.playhead) ? cmd.keys : null;
  return { label: words, keys, disabled: state === true ? "" : String(state), run: () => cmd.run(c) };
}

// The palette's context for a command: the selected track, or for S and I
// the track under the playhead.
export function paletteContext(cmd) {
  return cmd.playhead ? { track: keptTrackAt(now()), at: now() } : { track: S.selectedTrack, at: now() };
}

// ---- the context menus ----
// Which commands each surface offers, in order; "-" is a separator.
const MENUS = {
  block: ["play-here", "play-start", "-", "split", "identify", "-", "rename", "merge-prev", "merge-next", "restore",
    "-", "artwork", "cover", "-", "zoom-track", "copy-time"],
  row: ["play-start", "-", "split", "identify", "start-here", "-", "rename", "insert", "merge-prev", "merge-next", "restore",
    "-", "artwork", "cover", "-", "zoom-track", "copy-time"],
  pin: ["play-sample", "use-name", "-", "copy-time"],
  moment: ["ask", "play-here", "-", "copy-time"],
};
export const MENU_IDS = MENUS;

function menuItems(kind, c, heading) {
  const items = [{ heading }];
  for (const id of MENUS[kind]) {
    if (id === "-") { items.push("-"); continue; }
    const r = resolve(byId(id), c, "menu");
    if (r) items.push(r);
  }
  return items;
}

const where = (x, y) => ({ x, y });

// The track's menu, from its row (or the keyboard, anchored under the row).
export function rowMenu(t, at) {
  if (!t) return false;
  const p = now(), w = splitWindow(t);
  // The playhead is the moment (Split, Identify); "Copy" copies it while it's
  // inside the track, else the track's start.
  const c = { track: t, at: p, copyAt: w && p >= w.start && p < w.end ? p : startOf(t), pointer: false };
  return openMenu(menuItems("row", c, trackName(t) + " · " + TL.fmt(startOf(t))), at);
}

function rowAnchor(t) {
  const row = t && document.querySelector('#list .row[data-key="' + keyOf(t) + '"]');
  if (!row) return null;
  const r = row.getBoundingClientRect();
  return { x: r.left + 40, y: r.bottom, keyboard: true };
}

// ⇧F10 and the menu key: the selected track's menu, under its row.
export function selectedMenu() {
  const t = S.selectedTrack;
  const at = rowAnchor(t);
  if (!at) return false;
  return rowMenu(t, at);
}

// Right-click over the timeline and the list. Anywhere else the browser's own
// menu stays, and inside a text field too, for copy and paste.
document.addEventListener("contextmenu", e => {
  const surface = e.target.closest("#tl-rows, #list");
  if (!surface || e.target.closest("input, textarea")) return;
  e.preventDefault();
  // The menu key fires keydown, which opened the menu, and then this.
  if (dialogOpen() || menuJustOpened()) return;
  const at = where(e.clientX, e.clientY);
  const row = e.target.closest("#list .row");
  if (row) {
    const t = S.tracks.find(x => String(keyOf(x)) === row.dataset.key);
    if (!t) return;
    selectTrack(t, { keepTab: true });
    focusSelected();  // where focus goes back to when the menu closes
    rowMenu(t, at);
    return;
  }
  if (!e.target.closest("#tl-rows")) return;
  const pin = e.target.closest(".tl-pin[data-t]");
  if (pin) {
    const probe = S.probes.find(p => p.t === +pin.dataset.t);
    if (!probe) return;
    const c = { probe, track: keptTrackAt(probe.t + probe.window / 2), at: probe.t, pointer: true };
    const hit = probe.artist || probe.title;
    openMenu(menuItems("pin", c, (hit ? (probe.artist || "?") + " — " + (probe.title || "?") : "Shazam heard nothing")
      + " · " + TL.fmt(probe.t) + (probe.confidence ? " · " + probe.confidence : "")), at);
    return;
  }
  const block = e.target.closest(".tl-seg[data-i], .tl-edge[data-i]");
  const seconds = timeAt(e) ?? miniTime(e);
  if (block) {
    const t = S.tracks[+block.dataset.i];
    if (!t || seconds === null) return;
    selectTrack(t, { keepTab: true });
    focusSelected();
    openMenu(menuItems("block", { track: t, at: seconds, pointer: true }, trackName(t) + " · " + TL.fmt(seconds)), at);
    return;
  }
  if (seconds === null) return;
  openMenu(menuItems("moment", { track: keptTrackAt(seconds), at: seconds, pointer: true }, "At " + TL.fmt(seconds)), at);
});

// A moment on the overview strip, which spans the whole set.
function miniTime(e) {
  const mini = e.target.closest("#tl-mini");
  if (!mini) return null;
  const r = mini.getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * setDuration();
}
