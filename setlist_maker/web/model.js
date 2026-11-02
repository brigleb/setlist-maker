// Derivations over S.tracks: track windows, which track holds a moment.

import { S, TL } from "./state.js";
import { audio } from "./dom.js";
import { fmt } from "./util.js";

// A track's window runs from its own timestamp to the next track's; the last
// runs to the end of the audio. Derived on demand, never cached, so inserting
// a row needs no separate bookkeeping. Clamped to >=1s so two tracks sharing a
// timestamp cannot produce a zero-width scrubber and a division by zero.
export function windowFor(i) {
  const t = S.tracks[i];
  if (!t) return { start: 0, end: 1, length: 1 };
  const start = t.timestamp;
  const next = S.tracks[i + 1];
  const rawEnd = next ? next.timestamp : (audio.duration || start + 1);
  const end = Math.max(rawEnd, start + 1);
  return { start, end, length: end - start };
}

// Which track's window contains this playback position. Returns the last
// track for a position past the final boundary, 0 for a position before the
// first track's timestamp (a set whose first track does not start at 0).
// The playing track keeps winning while the position is still inside its
// own window: the backwards walk resolves two tracks sharing a timestamp to
// the *later* one, which would otherwise re-scope (and scroll) away from
// the earlier duplicate on the first timeupdate tick after selecting it,
// leaving that row impossible to select at all.
export function trackIndexAt(seconds) {
  if (!S.tracks.length) return null;
  const p = playingIndex();
  if (p !== null) {
    const w = windowFor(p);
    if (seconds >= w.start && seconds < w.end) return p;
  }
  for (let i = S.tracks.length - 1; i >= 0; i--) {
    if (seconds >= S.tracks[i].timestamp) return i;
  }
  return 0;
}

// "0:43 in" / "3:17 left" for the position within track i's window.
export function fmtWindowTimes(i, seconds) {
  const w = windowFor(i);
  const into = Math.max(0, Math.min(seconds - w.start, w.length));
  return { into: fmt(into) + " in", left: fmt(w.length - into) + " left" };
}

// The playing track's position, or null. Derived from the object reference
// every time, so a splice or a re-sort can never leave it pointing at another
// track. A track that is no longer in the list (undone, reloaded) plays nothing.
export function playingIndex() {
  const i = S.playingTrack ? S.tracks.indexOf(S.playingTrack) : -1;
  return i === -1 ? null : i;
}

export function setDuration() {
  return audio.duration || S.probeDuration || (S.tracks.length ? S.tracks[S.tracks.length - 1].timestamp + 60 : 60);
}

// Cut the selected track at the playhead: the new row starts where the user
// is listening, blank, and is selected so it can be named straight away.
// The span "Split at playhead" may cut: the track's kept window (so a merged
// track can be split anywhere it now covers), or a merged row's own span.
export function splitWindow(t) {
  const i = S.tracks.indexOf(t);
  if (i === -1) return null;
  const D = setDuration();
  return TL.keptWindows(S.tracks, D).find(x => x.index === i) || TL.windows(S.tracks, D)[i];
}

// The kept track whose span holds `seconds` -- what S splits while listening,
// which need not be the selected track (automatic advance does not select).
export function keptTrackAt(seconds) {
  const w = TL.keptWindows(S.tracks, setDuration()).find(x => seconds >= x.start && seconds < x.end);
  return w ? S.tracks[w.index] : null;
}

// Where track i may start: whole seconds, at least 1s after the row before it
// and 1s before the row after it -- rejected rows included, so moving a start
// never reorders the list (which would silently re-home merged rows, and give
// two tracks one timestamp, which the sidecar's join by timestamp cannot tell
// apart). The first track may start anywhere from 0. Every way of moving a
// start goes through here: dragging a seam, [ and ], and the Start fields.
// A row already on its neighbour's second (a merge into the next track, saved
// and reloaded, #56) may move away from it but is never pushed past it.
export function clampStart(i, seconds) {
  const t = S.tracks[i], prev = S.tracks[i - 1], next = S.tracks[i + 1];
  const lo = prev ? Math.min(prev.timestamp + 1, t.timestamp) : 0;
  const hi = next ? Math.max(next.timestamp - 1, t.timestamp) : Math.max(lo, Math.floor(setDuration()) - 1);
  return Math.max(lo, Math.min(hi, Math.round(seconds)));
}
