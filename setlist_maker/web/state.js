// Shared state. Everything more than one module reassigns lives on S, since an
// imported binding is read-only; the rest stays local to the module that owns it.

export const GRADS = [
  "linear-gradient(135deg,#6a5acd,#0a84ff)",
  "linear-gradient(135deg,#1f9d55,#7bd389)",
  "linear-gradient(135deg,#ff6b6b,#feca57)",
  "linear-gradient(135deg,#f857a6,#ff5858)",
  "linear-gradient(135deg,#43cea2,#185a9d)",
];
export const TL = window.Timeline;

export const S = {
  tracks: [],
  dirty: false,
  // The playing track's index, and the track *object* held alongside it.
  // addBelow()'s splice and commit()'s re-sort can both move the playing track
  // to a different array position without changing what's on screen for it --
  // reindexPlayingTrack() re-derives playingIndex from the reference, so a
  // stale bare index never points at a different track after either mutation.
  playingIndex: null,
  playingTrack: null,
  // Held as an object reference, like playingTrack and artwork.js's artTrack:
  // render() and the re-sorts that inserts cause move positions underneath it.
  selectedTrack: null,
  // The run's probes, drawn as pins; the audio's length as the run saw it.
  probes: [],
  probeDuration: null,
  canSample: false,  // the server has the audio and a sampler
  liveMode: false,   // `identify --watch` and the run has not finished
  livePoll: null,
  artVersion: 0,  // bumped on save so re-requested thumbs bypass the decoded-image cache
  // The episode cover file <set>_cover.jpg: coverVersion is the saved file's
  // mtime (null: none), coverChoice the unsaved change -- undefined for none,
  // an upload reference for a new cover, null to remove it. coverChoice is part
  // of the undo snapshot.
  coverVersion: null,
  coverChoice: undefined,
  canEmbed: false,
};
