//! Hindsight's core: everything with no UI.
//!
//! `model` is what a set is and how it is stored on disk (M1). `engine` folds a run's probes
//! into a tracklist and says where to probe next (M2). The audio, Shazam, artwork and chapter
//! modules arrive with later milestones.

pub mod engine;
pub mod model;
