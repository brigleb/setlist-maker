//! What a set is and how it is stored: the tracklist, its markdown and sidecar, corrections,
//! the episode's tags, uploaded images and the progress file. Every front end reads and
//! writes sets through here.

pub mod corrections;
pub mod edit;
pub mod fileio;
pub mod markdown;
pub mod store;
pub mod track;

pub use corrections::CorrectionsDb;
pub use edit::{apply_track_edit, strip_reissue_tag};
pub use markdown::{ParseWarning, Parsed, parse_markdown_tracklist};
pub use store::{find_audio_file, load_tracklist, resolve_audio_path, save_tracklist};
pub use track::{Track, Tracklist};
