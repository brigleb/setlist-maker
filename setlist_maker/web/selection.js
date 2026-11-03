// Which tracks are selected (#77). S.selectedTrack is the one the inspector
// shows, the roving tab stop and what the single-track keys act on; S.selected
// holds the rest of a multiple selection beside it. Imports only state.js, so
// it is safe from the module cycles (see CLAUDE.md).

import { S } from "./state.js";

export const isSelected = t => !!t && (t === S.selectedTrack || S.selected.has(t));

// The selected tracks in list order. Filtering S.tracks also drops any that an
// undo took out of the list.
export function selection() {
  if (!S.selected.size) return S.selectedTrack ? [S.selectedTrack] : [];
  return S.tracks.filter(t => S.selected.has(t));
}

export const isMulti = () => selection().length > 1;

// How a click modifies the selection: ⇧ extends from the anchor, ⌘ (Ctrl)
// adds or removes one track. Null for a plain click.
export const clickMods = e => (e.shiftKey ? { extend: true } : e.metaKey || e.ctrlKey ? { toggle: true } : null);

// Point the selection at t. A plain call selects t alone; { extend } selects
// every row from the anchor to t; { toggle } adds or removes t; { keepMulti }
// moves the focus to t without dropping a multiple selection t is part of (a
// right-click on a selected row). Returns the track to show, which a toggle
// that removed the shown track moves to its nearest selected neighbour.
export function pointSelection(t, opts = {}) {
  if (opts.extend) {
    const a = isSelected(S.anchor) ? S.anchor : S.selectedTrack || t;
    const i = S.tracks.indexOf(a), j = S.tracks.indexOf(t);
    S.anchor = a;
    S.selected = i === -1 || i === j ? new Set() : new Set(S.tracks.slice(Math.min(i, j), Math.max(i, j) + 1));
    return t;
  }
  if (opts.toggle) {
    const set = new Set(selection());
    let show = t;
    if (set.has(t) && set.size > 1) {
      set.delete(t);
      const i = S.tracks.indexOf(t);
      show = [...set].sort((a, b) => Math.abs(S.tracks.indexOf(a) - i) - Math.abs(S.tracks.indexOf(b) - i))[0];
    } else set.add(t);
    S.selected = set.size > 1 ? set : new Set();
    S.anchor = show;
    return show;
  }
  if (!(opts.keepMulti && S.selected.has(t))) { S.selected = new Set(); S.anchor = t; }
  return t;
}

export function clearMulti() { S.selected = new Set(); S.anchor = S.selectedTrack; }
