//! Chapter markers in the MP3: the ID3v2.3 embed, and what a tracklist puts in it.

mod embed;
#[cfg(test)]
mod raw;
#[cfg(test)]
mod tests;
pub mod tracklist;

pub use embed::{Chapter, EmbedError, Episode, MAX_CHAPTERS, Written, embed};
pub use tracklist::{Art, CoverFrom, Prepared, Progress, chapter_title, prepare};
