//! Hindsight's core: everything with no UI.
//!
//! `model` is what a set is and how it is stored on disk (M1). `engine` folds a run's probes
//! into a tracklist and says where to probe next (M2). `audio` decodes windows of a recording,
//! `shazam` fingerprints them and asks Shazam, and `run` drives a whole identification (M3).
//! Artwork and chapters arrive with M4.

pub mod audio;
pub mod engine;
pub mod model;
pub mod run;
pub mod shazam;
pub mod summary;
