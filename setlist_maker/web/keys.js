// The page's keyboard map (#58).
//
// One table, KEYMAP, is both the dispatcher's list and the shortcut sheet's
// source (? opens it), so the sheet cannot drift from what the keys do. Each
// entry has the keys as the sheet shows them, a label, a match on the event,
// an optional gate (`audio`: the player is ready; `select`: a track is
// selected) and the action. The first entry that matches wins, so a
// modified key (⇧Space) sits above its plain one. An action that returns
// false let the key through to the browser.

import { S } from "./state.js";
import { audio, el, playerEl, pplayEl, toast } from "./dom.js";
import { keptTrackAt } from "./model.js";
import { redo, undo } from "./history.js";
import { nextTrack, playSelected, prevTrack, shuttle, skip, toggleFollow } from "./player.js";
import { mergeInto, moveStart, splitAt, startOf } from "./edits.js";
import { identifyAt } from "./inspector.js";
import { dialogOpen, sheet } from "./dialog.js";
import { selectTrack, zoomAll, zoomBy, zoomToTrack } from "./grid.js";
import { focusSelected, startEdit } from "./list.js";
import { save } from "./main.js";
import { openPalette } from "./palette.js";
import { selectedMenu } from "./commands.js";

// Matches a key with exactly these modifiers. ⌘ and Ctrl count as one.
const key = (names, mods = {}) => e => [].concat(names).includes(e.key)
  && !!e.shiftKey === !!mods.shift && !!e.altKey === !!mods.alt
  && !!(e.metaKey || e.ctrlKey) === !!mods.cmd;
// A letter either case (caps lock, or ⇧ held by habit), with no ⌥ or ⌘.
const letter = c => e => e.key.toLowerCase() === c && !e.altKey && !e.metaKey && !e.ctrlKey;
// Punctuation arrives already shifted ({ for ⇧[, ? for ⇧/), so ⇧ is ignored.
const punct = names => e => names.includes(e.key) && !e.altKey && !e.metaKey && !e.ctrlKey;

// A focused button keeps Space and Enter, so Tab to Save and Enter still
// saves. #pplay is the play button: Space there is play/pause either way.
const onButton = () => {
  const f = document.activeElement;
  return !!f && f.tagName === "BUTTON" && f.id !== "pplay";
};

export const KEYMAP = [
  // ---- tracks ----
  { group: "Tracks", keys: ["↑", "↓"], label: "Select the previous / next track",
    match: key(["ArrowUp", "ArrowDown"]), run: e => moveSelection(e.key === "ArrowUp" ? -1 : 1) },
  { group: "Tracks", keys: ["Enter", "F2"], label: "Rename the selected track (Enter moves to the next field, Esc cancels)",
    match: key(["Enter", "F2"]), need: "select",
    run: e => { if (e.key === "Enter" && onButton()) return false; startEdit(S.selectedTrack, "artist", { select: true }); } },
  { group: "Tracks", keys: ["⇧⌫"], label: "Merge the selected track into the next one",
    match: key(["Backspace", "Delete"], { shift: true }), need: "select", run: () => mergeInto(S.selectedTrack, "next") },
  { group: "Tracks", keys: ["⌫"], label: "Merge the selected track into the previous one; on a merged track, bring it back",
    match: key(["Backspace", "Delete"]), need: "select", run: () => mergeInto(S.selectedTrack, "prev") },
  { group: "Tracks", keys: ["⇧F10"], label: "The selected track's actions (the menu key too)",
    match: e => e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey),
    need: "select", run: () => { selectedMenu(); } },
  { group: "Tracks", keys: ["[", "]"], label: "Move the selected track's start 1s earlier / later (⇧ for 5s)",
    match: punct(["[", "]", "{", "}"]), need: "select", run: e => nudge({ "[": -1, "]": 1, "{": -5, "}": 5 }[e.key]) },
  { group: "Tracks", keys: ["S"], label: "Split at the playhead",
    match: letter("s"), run: () => {
      const t = keptTrackAt(audio.currentTime || 0);
      if (!t || !splitAt(t, audio.currentTime || 0)) return false;
      focusSelected();  // the new track, so ↑/↓ and Enter carry on from it
    } },
  { group: "Tracks", keys: ["I"], label: "Identify at the playhead (asks Shazam)",
    match: letter("i"), run: () => {
      if (!S.canSample || S.liveMode) return false;
      identifyAt(keptTrackAt(audio.currentTime || 0), audio.currentTime || 0);
    } },
  // ---- playback ----
  { group: "Playback", keys: ["⇧Space"], label: "Play the selected track from its start",
    match: key(" ", { shift: true }), need: "audio", run: () => { if (onButton()) return false; playSelected(); } },
  { group: "Playback", keys: ["Space"], label: "Play / pause",
    match: key(" "), need: "audio", run: () => { if (onButton()) return false; pplayEl.click(); } },
  { group: "Playback", keys: ["←", "→"], label: "Back / forward 5s (⇧ for 15s, ⌥ for 1s)",
    match: e => (e.key === "ArrowLeft" || e.key === "ArrowRight") && !e.metaKey && !e.ctrlKey && !(e.shiftKey && e.altKey),
    need: "audio", run: e => skip((e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 15 : e.altKey ? 1 : 5)) },
  { group: "Playback", keys: ["⌥↑", "⌥↓"], label: "Play the previous / next track",
    match: key(["ArrowUp", "ArrowDown"], { alt: true }), need: "audio",
    run: e => (e.key === "ArrowUp" ? prevTrack() : nextTrack()) },
  { group: "Playback", keys: ["J", "K", "L"], label: "Shuttle: slower or back 5s / pause / play, again for faster",
    match: e => ["j", "k", "l"].some(c => letter(c)(e)), need: "audio", run: e => shuttle(e.key.toLowerCase()) },
  { group: "Playback", keys: ["F"], label: "Follow the playhead: the selection moves with the audio",
    match: letter("f"), run: () => toggleFollow() },
  // ---- timeline (#57) ----
  { group: "Timeline", keys: ["Z"], label: "Zoom to the selected track",
    match: letter("z"), run: () => zoomToTrack(S.selectedTrack) },
  { group: "Timeline", keys: ["0"], label: "Show the whole set", match: punct(["0"]), run: () => zoomAll() },
  { group: "Timeline", keys: ["=", "-"], label: "Zoom in / out",
    match: punct(["=", "+", "-"]), run: e => zoomBy(e.key === "-" ? 2 : 0.5) },
  // ---- the set ----
  // ⌘K, ⌘S and ⌘Z are handled ahead of the table (see the handler); they are
  // listed here so the sheet shows them.
  { group: "Set", keys: ["⌘K"], label: "Find a command, a track, or a time like 1:23:45", match: () => false },
  { group: "Set", keys: ["⌘S"], label: "Save", match: () => false },
  { group: "Set", keys: ["⌘Z", "⇧⌘Z"], label: "Undo / redo", match: () => false },
  { group: "Set", keys: ["?"], label: "Show these shortcuts", match: punct(["?"]), run: () => openSheet() },
  { group: "Set", keys: ["Esc"], label: "Close a panel, cancel an edit or a drag", match: () => false },
];

// What the mouse does, for the sheet. It used to be the grey line under the
// timeline.
export const MOUSE = [
  ["⌘-scroll or pinch", "Zoom the timeline"],
  ["Scroll", "Pan the timeline"],
  ["Click a track", "Select it"],
  ["Click its artist, title or start", "Edit it in place"],
  ["Double-click the timeline", "Play from there"],
  ["⌥-click a track", "Split it there"],
  ["Drag a track's left edge", "Move its start (⌥: no snapping)"],
  ["Click a pin", "Hear what Shazam heard"],
  ["Click the empty pin lane", "Ask Shazam about that moment"],
  ["Click a thumbnail", "Artwork and episode cover"],
  ["Right-click a track, a sample or the timeline", "Its actions"],
];

function moveSelection(d) {
  if (!S.tracks.length) return;
  const cur = S.tracks.indexOf(S.selectedTrack);
  // Nothing selected yet: start from the playing track, else an end.
  const from = cur !== -1 ? cur : S.tracks.indexOf(S.playingTrack);
  const i = from === -1 ? (d > 0 ? 0 : S.tracks.length - 1)
    : cur === -1 ? from : Math.max(0, Math.min(S.tracks.length - 1, cur + d));
  selectTrack(S.tracks[i]);
  focusSelected();
}

function nudge(by) {
  const t = S.selectedTrack;
  if (S.liveMode) return;
  if (!moveStart(t, startOf(t) + by)) toast("It can't move any further: it would pass the track next to it");
}

// ---- the shortcut sheet ----
const sheetDlg = sheet(document.getElementById("keys-dialog"));
let sheetBuilt = false;

// Built on first open, not at load: module-load calls into other modules
// are the cycle trap (see CLAUDE.md).
function buildSheet() {
  const body = document.getElementById("keys-body");
  const groups = [];
  for (const k of KEYMAP) {
    let g = groups.find(x => x.name === k.group);
    if (!g) groups.push(g = { name: k.group, rows: [] });
    g.rows.push([k.keys, k.label]);
  }
  groups.push({ name: "Mouse", rows: MOUSE.map(([how, what]) => [[how], what]) });
  for (const g of groups) {
    const sec = el("section", "keys-group");
    sec.appendChild(el("h3", null, g.name));
    const dl = el("dl");
    for (const [keys, label] of g.rows) {
      const dt = el("dt");
      keys.forEach(k => dt.appendChild(el("kbd", null, k)));
      dl.appendChild(dt);
      dl.appendChild(el("dd", null, label));
    }
    sec.appendChild(dl);
    body.appendChild(sec);
  }
  sheetBuilt = true;
}

export function openSheet() {
  if (!sheetBuilt) buildSheet();
  sheetDlg.showModal();
}

// ? closes it again, as it opened it. Escape is the dialog's own. Stopped
// here, or the document's handler, finding no dialog open any more, would
// read the same ? as "open the sheet".
sheetDlg.addEventListener("keydown", e => {
  if (e.key === "?") { e.preventDefault(); e.stopPropagation(); sheetDlg.close(); }
});
document.getElementById("keys-btn").onclick = () => openSheet();

// ---- the handler ----
document.addEventListener("keydown", (e) => {
  // A dialog owns the keyboard while it is open (#61): the shortcut sheet, the
  // artwork picker, ask()'s questions, the palette and the context menu (#59)
  // handle their own keys (so ⌘K does nothing over the picker), and the
  // focus trap means a key pressed there still bubbles up to here.
  if (dialogOpen()) return;
  // Never steal keys from an editor: artist/title/time fields and the summary
  // textarea all need Space and the arrows to behave normally. #seek is an
  // INPUT too, but deliberately excluded -- e.preventDefault() below already
  // blocks its native arrow-key stepping, so arrows should seek rather than
  // get eaten here just because the scrubber has focus.
  // tagName is enough because this page has no contenteditable surface (and
  // no shadow root that could hide one behind its host). Anyone adding a
  // rich-text field must extend this guard (e.g. el.isContentEditable), or
  // Space and the arrows will be eaten while they type.
  // The event's target counts as well as the focus: a field's own Enter or
  // Escape can move focus to a row before the event bubbles up to here, and
  // that keypress must not then rename the row as well.
  const isField = n => !!n && (n.tagName === "TEXTAREA" || (n.tagName === "INPUT" && n.type !== "range"));
  const f = document.activeElement;
  const typing = isField(f) || isField(e.target);
  // ⌘K opens the palette from anywhere, a text field included (#59). A field
  // being edited commits as focus leaves it for the palette, as on any blur.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openPalette();
    return;
  }
  // ⌘S saves from anywhere, a text field included: the browser's own ⌘S
  // saves the page. A field holds its text as a draft until it loses focus
  // (the list's commits on a timeout), so blur it and save after that.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (S.liveMode) return;
    if (isField(f)) f.blur();
    setTimeout(() => save(), 0);
    return;
  }
  if (typing) return;
  // Past the guard above, so ⌘Z inside a text field stays the field's own undo.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "z") {
    e.preventDefault();
    if (e.shiftKey) redo(); else undo();
    return;
  }
  for (const k of KEYMAP) {
    if (!k.match(e)) continue;
    if (k.need === "audio" && playerEl.classList.contains("disabled")) return;
    if (k.need === "select" && !S.selectedTrack) return;
    if (k.run(e) !== false) e.preventDefault();
    return;
  }
});
