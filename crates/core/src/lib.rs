//! Hindsight's core: everything with no UI.
//!
//! `model` is what a set is and how it is stored on disk (M1). `engine` folds a run's probes
//! into a tracklist and says where to probe next (M2). `audio` decodes windows of a recording,
//! `shazam` fingerprints them and asks Shazam, and `run` drives a whole identification (M3).
//! `artwork` finds a track's cover and `chapters` embeds the tracklist in the MP3 (M4).

pub mod artwork;
pub mod audio;
pub mod chapters;
pub mod engine;
pub mod model;
pub mod run;
pub mod shazam;
pub mod summary;
