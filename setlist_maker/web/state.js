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
  fileName: "",       // the set's audio file, for the toolbar and the tab title
  savedAt: null,      // when this page last saved (Date.now()), for "Saved 2 min ago"
  // The playing, selected and inline-edited tracks, all held as object
  // references: an insert or a re-sort moves array positions, never objects.
  // model.playingIndex() derives the playing track's position when needed.
  playingTrack: null,
  selectedTrack: null,
  // A multiple selection (#77): every selected track, selectedTrack among
  // them, or empty when only selectedTrack is. selectedTrack stays the one the
  // inspector shows and the keys act on; anchor is where a ⇧-range starts.
  selected: new Set(),
  anchor: null,
  editing: null,      // the track being edited in place in the list
  editField: "artist", // ...and the cell that was clicked to start it
  editSelect: false,  // ...and whether its text starts selected (from the keyboard)
  follow: false,      // the selection follows the playing track (#58, F)
  // The run's probes, drawn as pins; the audio's length as the run saw it.
  probes: [],
  probeDuration: null,
  canSample: false,  // the server has the audio and a sampler
  // The waveform (#57): { rate, levels } from /api/peaks, null until it arrives.
  peaks: null,
  liveMode: false,   // `identify --watch` and the run has not finished
  livePoll: null,
  // The app (Hindsight) only, see app.js: its editor answers /api/tracklist
  // with `open`; a run that stopped early and can carry on; why a live run is
  // paused (Shazam throttling); whether the claude CLI was found.
  app: false,
  run: null,
  paused: null,
  summaryAvailable: undefined,
  artVersion: 0,  // bumped on save so re-requested thumbs bypass the decoded-image cache
  // The episode cover file <set>_cover.jpg: coverVersion is the saved file's
  // mtime (null: none), coverChoice the unsaved change -- undefined for none,
  // an upload reference for a new cover, null to remove it. coverChoice is part
  // of the undo snapshot.
  coverVersion: null,
  coverChoice: undefined,
  canEmbed: false,
};
