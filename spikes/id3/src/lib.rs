//! M0 spike 4: write the chapter tag `chapters.py` writes, through the `id3` crate.
//!
//! The frame shape only, not a port of `embed_chapters_for_tracklist`: no cover precedence and
//! no artwork lookup. `raw` reads the bytes back on its own, so the tests don't trust the
//! writer to check itself.

use std::fs::{File, OpenOptions};
use std::io::{BufReader, Read, Seek, SeekFrom, Write};
use std::path::Path;

use id3::frame::{Chapter as Chap, Picture, PictureType, TableOfContents};
use id3::{Encoder, Encoding, Frame, Tag, TagLike, Version};
use serde::Deserialize;

pub mod raw;

/// One chapter as the embed receives it: kept tracks, in the order they play.
#[derive(Clone, Debug, Deserialize)]
pub struct Chapter {
    pub start_ms: u32,
    pub end_ms: u32,
    pub title: String,
    #[serde(skip)]
    pub image: Option<Vec<u8>>,
}

/// The set's own tags. A blank or missing value leaves what the file already has.
#[derive(Clone, Debug, Default)]
pub struct Episode {
    pub title: Option<String>,
    pub artist: Option<String>,
    pub cover: Option<Vec<u8>>,
}

/// "Not used" for a CHAP's byte offsets, as mutagen writes them.
const NO_OFFSET: u32 = u32::MAX;

fn jpeg(description: String, data: Vec<u8>) -> Picture {
    Picture {
        mime_type: "image/jpeg".into(),
        picture_type: PictureType::CoverFront,
        description,
        data,
    }
}

fn filled(v: &Option<String>) -> Option<&str> {
    v.as_deref().map(str::trim).filter(|s| !s.is_empty())
}

/// Replaces the file's chapters (and, when given, its cover and episode text) and writes the
/// tag as ID3v2.3. Chapters land in the tag in the order given.
pub fn embed(path: &Path, episode: &Episode, chapters: &[Chapter]) -> id3::Result<()> {
    // CTOC counts its children in one byte, and the crate writes `len() as u8` without a check.
    if chapters.len() > u8::MAX as usize {
        return Err(id3::Error::new(
            id3::ErrorKind::InvalidInput,
            "a CTOC holds at most 255 chapters",
        ));
    }
    let tag = match Tag::read_from_path(path) {
        Ok(t) => t,
        Err(e) if matches!(e.kind, id3::ErrorKind::NoTag) => Tag::new(),
        Err(e) => return Err(e),
    };
    // v2.3 has only Latin-1 and UTF-16. A frame carried over from a v2.4 tag keeps the UTF-8
    // (or UTF-16BE) it was read with, and the crate writes that byte into a v2.3 tag as is.
    let mut tag: Tag = tag
        .frames()
        .cloned()
        .map(|f| match f.encoding() {
            Some(Encoding::UTF8 | Encoding::UTF16BE) => f.set_encoding(Some(Encoding::UTF16)),
            _ => f,
        })
        .collect();
    tag.remove("CHAP");
    tag.remove("CTOC");
    if let Some(cover) = &episode.cover {
        tag.remove_all_pictures();
        tag.add_frame(jpeg("Episode Cover".into(), cover.clone()));
    }
    if let Some(title) = filled(&episode.title) {
        tag.set_title(title);
        tag.set_album(title);
    }
    if let Some(artist) = filled(&episode.artist) {
        tag.set_artist(artist);
    }

    let ids: Vec<String> = (0..chapters.len()).map(|i| format!("chp{i:03}")).collect();
    for (i, (c, id)) in chapters.iter().zip(&ids).enumerate() {
        let mut frames = vec![Frame::text("TIT2", c.title.clone())];
        if let Some(img) = &c.image {
            frames.push(jpeg(format!("Chapter {}", i + 1), img.clone()).into());
        }
        tag.add_frame(Chap {
            element_id: id.clone(),
            start_time: c.start_ms,
            end_time: c.end_ms,
            start_offset: NO_OFFSET,
            end_offset: NO_OFFSET,
            frames,
        });
    }
    tag.add_frame(TableOfContents {
        element_id: "toc".into(),
        top_level: true,
        ordered: true,
        elements: ids,
        frames: vec![Frame::text("TIT2", "Table of Contents")],
    });
    write(&tag, path)
}

/// The room the file's tag takes now: its declared size plus any zeros after it, which is the
/// region the crate replaces (`locate_id3v2`).
fn region(file: &mut File) -> std::io::Result<u64> {
    let mut head = [0; 10];
    if file.read_exact(&mut head).is_err() {
        return Ok(0);
    }
    let declared = raw::tag_len(&head) as u64;
    if declared == 0 {
        return Ok(0);
    }
    file.seek(SeekFrom::Start(declared))?;
    let zeros = BufReader::new(&mut *file)
        .bytes()
        .take_while(|b| matches!(b, Ok(0)))
        .count() as u64;
    Ok(declared + zeros)
}

/// Writes the tag as v2.3 with mutagen's padding rule (`PaddingInfo._get_default_padding`): if
/// it fits in the room the old tag had, the rest becomes padding and the audio stays put;
/// otherwise (or if that would leave more than 10KB + 1% of the audio) the padding is
/// 1KB + 0.1% of the audio, and the audio moves once.
fn write(tag: &Tag, path: &Path) -> id3::Result<()> {
    let mut file = OpenOptions::new().read(true).write(true).open(path)?;
    let size = file.metadata()?.len();
    let room = region(&mut file)?;
    file.seek(SeekFrom::Start(0))?;
    let mut bare = Vec::new();
    Encoder::new()
        .version(Version::Id3v23)
        .encode(tag, &mut bare)?;
    let need = bare.len() as u64;
    let audio = size - room;
    let (low, high) = (1024 + audio / 1000, 10 * 1024 + audio / 100);
    let padding = match room.checked_sub(need) {
        Some(spare) if spare <= high => spare,
        _ => low,
    };
    Encoder::new()
        .version(Version::Id3v23)
        .padding(padding as usize)
        .write_to_file(tag, &mut file)?;
    file.flush()?;
    Ok(())
}
