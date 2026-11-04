//! The ID3v2.3 chapter tag: CHAP/CTOC with per-chapter APIC, the episode's cover and text.
//!
//! Through the `id3` crate (spike 4), with three things it doesn't do itself:
//! - **v2.3 only (#17).** A tag read as v2.4 is converted first, as mutagen's `update_to_v23`
//!   does ([`to_v23`]), and frames that kept UTF-8 or UTF-16BE are re-marked UTF-16, since v2.3
//!   has neither and the crate would write the byte as is.
//! - **At most 255 chapters**: CTOC counts its children in one byte, and the crate writes
//!   `len() as u8` unchecked.
//! - **mutagen's padding rule**, so a re-embed leaves the audio where it is. When the tag
//!   outgrows its room, the file is rewritten beside itself and renamed over, rather than the
//!   crate's in-place move, which a crash or sleep partway through would leave half-moved.
//!
//! The crate writes frames in the order they're added, so chapters added in time order sit in
//! time order on disk, which is what ffprobe lists (#33). Nothing reorders them afterwards.

use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, Read, Seek, SeekFrom, Write};
use std::path::Path;

use id3::frame::{Chapter as Chap, Content, Picture, PictureType, TableOfContents};
use id3::{Encoder, Encoding, Frame, Tag, TagLike, Version};

use crate::model::fileio::atomic_replace;

/// One chapter: a kept track, in play order.
#[derive(Clone, Debug, PartialEq)]
pub struct Chapter {
    pub start_ms: u32,
    pub end_ms: u32,
    pub title: String,
    /// JPEG bytes, or nothing: a chapter with no art gets no APIC.
    pub image: Option<Vec<u8>>,
}

/// The set's own tags. A blank or missing value leaves what the file already has.
#[derive(Clone, Debug, Default)]
pub struct Episode {
    pub title: Option<String>,
    pub artist: Option<String>,
    /// Replaces every top-level picture when given.
    pub cover: Option<Vec<u8>>,
}

/// The most chapters a CTOC can list.
pub const MAX_CHAPTERS: usize = u8::MAX as usize;

/// "Not used" for a CHAP's byte offsets, as mutagen writes them.
const NO_OFFSET: u32 = u32::MAX;

/// How the tag reached the disk.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Written {
    /// Into the room the old tag had; the audio didn't move.
    InPlace,
    /// The tag needed more room (or far less): the file was rewritten beside itself, with
    /// padding for next time, and renamed over the original.
    Rewritten,
}

#[derive(Debug)]
pub enum EmbedError {
    NoChapters,
    TooManyChapters(usize),
    /// The file's tag claims more bytes than the file has: cut short, or still downloading.
    TagPastEnd,
    Tag(id3::Error),
    Io(io::Error),
}

impl fmt::Display for EmbedError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EmbedError::NoChapters => write!(f, "no tracks to embed as chapters"),
            EmbedError::TooManyChapters(n) => write!(
                f,
                "{n} chapters is more than an MP3's table of contents can list ({MAX_CHAPTERS})"
            ),
            EmbedError::TagPastEnd => write!(
                f,
                "the MP3's tag claims more bytes than the file has (still downloading?)"
            ),
            EmbedError::Tag(e) => write!(f, "can't read the MP3's tag: {e}"),
            EmbedError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for EmbedError {}

impl From<io::Error> for EmbedError {
    fn from(e: io::Error) -> Self {
        EmbedError::Io(e)
    }
}

impl From<id3::Error> for EmbedError {
    fn from(e: id3::Error) -> Self {
        match e.kind {
            id3::ErrorKind::Io(io) => EmbedError::Io(io),
            _ => EmbedError::Tag(e),
        }
    }
}

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
/// tag as ID3v2.3. Chapters land in the tag in the order given; the caller sorts them.
pub fn embed(path: &Path, episode: &Episode, chapters: &[Chapter]) -> Result<Written, EmbedError> {
    if chapters.is_empty() {
        return Err(EmbedError::NoChapters);
    }
    if chapters.len() > MAX_CHAPTERS {
        return Err(EmbedError::TooManyChapters(chapters.len()));
    }
    let read = match Tag::read_from_path(path) {
        Ok(t) => t,
        Err(e) if matches!(e.kind, id3::ErrorKind::NoTag) => Tag::new(),
        Err(e) => return Err(e.into()),
    };
    let mut tag = to_v23(&read);
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

/// Frames that exist only in v2.4, which mutagen's `update_to_v23` deletes.
const V24_ONLY: [&str; 18] = [
    "ASPI", "EQU2", "RVA2", "SEEK", "SIGN", "TDEN", "TDOR", "TDRC", "TDRL", "TDTG", "TIPL", "TMCL",
    "TMOO", "TPRO", "TSOA", "TSOP", "TSOT", "TSST",
];

/// The tag's frames as v2.3 can hold them.
///
/// From a v2.4 tag, as mutagen's `update_to_v23`: the recording date `TDRC` becomes `TYER`
/// (and `TDAT`/`TIME` when it has them), `TDOR` becomes `TORY`, the other v2.4-only frames go,
/// and text loses the trailing NUL v2.4 ends values with (the crate would write it as `/`).
/// `TIPL`/`TMCL` are dropped rather than merged into `IPLS`. Any tag: UTF-8 and UTF-16BE, which
/// v2.3 lacks, become UTF-16.
fn to_v23(read: &Tag) -> Tag {
    let v24 = read.version() == Version::Id3v24;
    let text = |id: &str| read.get(id).and_then(|f| f.content().text()).map(trim_nul);
    let mut added = Vec::new();
    if v24 {
        if let Some(d) = text("TDRC").map(parse_date) {
            if let Some(y) = d.year.filter(|_| read.get("TYER").is_none()) {
                added.push(Frame::text("TYER", format!("{y:04}")));
            }
            if let (Some(m), Some(day)) = (d.month, d.day)
                && read.get("TDAT").is_none()
            {
                added.push(Frame::text("TDAT", format!("{day:02}{m:02}")));
            }
            // mutagen's `d.hour and d.minute`: a zero hour or minute writes no TIME.
            if let (Some(h @ 1..), Some(mi @ 1..)) = (d.hour, d.minute)
                && read.get("TIME").is_none()
            {
                added.push(Frame::text("TIME", format!("{h:02}{mi:02}")));
            }
        }
        if let Some(y) = text("TDOR").and_then(|t| parse_date(t).year)
            && read.get("TORY").is_none()
        {
            added.push(Frame::text("TORY", format!("{y:04}")));
        }
    }
    read.frames()
        .filter(|f| !(v24 && V24_ONLY.contains(&f.id())))
        .cloned()
        .map(|f| match (v24, f.content()) {
            (true, Content::Text(t)) if t.ends_with('\0') => {
                let encoding = f.encoding();
                Frame::with_content(f.id(), Content::Text(trim_nul(t).to_string()))
                    .set_encoding(encoding)
            }
            _ => f,
        })
        .chain(added)
        .map(|f| match f.encoding() {
            Some(Encoding::UTF8 | Encoding::UTF16BE) => f.set_encoding(Some(Encoding::UTF16)),
            _ => f,
        })
        .collect()
}

fn trim_nul(t: &str) -> &str {
    t.trim_end_matches('\0')
}

#[derive(Debug, Default, PartialEq)]
struct Date {
    year: Option<u32>,
    month: Option<u32>,
    day: Option<u32>,
    hour: Option<u32>,
    minute: Option<u32>,
}

/// An ID3 timestamp, `yyyy[-MM[-dd[THH[:mm[:ss]]]]]`, as far as it parses.
fn parse_date(t: &str) -> Date {
    let num = |s: Option<&str>, len: usize| {
        s.filter(|s| s.len() == len && s.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|s| s.parse().ok())
    };
    let (date, time) = t.split_once('T').unwrap_or((t, ""));
    let mut d = date.split('-');
    let mut tm = time.split(':');
    let mut out = Date {
        year: num(d.next(), 4),
        ..Date::default()
    };
    out.month = out.year.and(num(d.next(), 2));
    out.day = out.month.and(num(d.next(), 2));
    out.hour = out.day.and(num(tm.next(), 2));
    out.minute = out.hour.and(num(tm.next(), 2));
    out
}

/// The room the file's tag takes now: its declared size plus any zeros after it, which is the
/// region the crate's own reader treats as the tag (`locate_id3v2`).
fn region(file: &mut File) -> io::Result<u64> {
    let mut head = [0; 10];
    if file.read_exact(&mut head).is_err() || &head[..3] != b"ID3" {
        return Ok(0);
    }
    let body = head[6..10]
        .iter()
        .fold(0u64, |n, &x| (n << 7) | u64::from(x & 0x7f));
    let footer = if head[5] & 0x10 != 0 { 10 } else { 0 };
    let declared = 10 + body + footer;
    file.seek(SeekFrom::Start(declared))?;
    let zeros = BufReader::new(&mut *file)
        .bytes()
        .take_while(|b| matches!(b, Ok(0)))
        .count() as u64;
    Ok(declared + zeros)
}

fn encode(tag: &Tag, padding: usize) -> Result<Vec<u8>, EmbedError> {
    let mut out = Vec::new();
    Encoder::new()
        .version(Version::Id3v23)
        .padding(padding)
        .encode(tag, &mut out)?;
    Ok(out)
}

/// mutagen's padding rule (`PaddingInfo._get_default_padding`): if the tag fits the room the
/// old one had, the rest becomes padding, unless that would leave more than 10KB + 1% of the
/// audio; otherwise the padding is 1KB + 0.1% of the audio.
fn padding(room: u64, need: u64, audio: u64) -> u64 {
    let (low, high) = (1024 + audio / 1000, 10 * 1024 + audio / 100);
    match room.checked_sub(need) {
        Some(spare) if spare <= high => spare,
        _ => low,
    }
}

fn write(tag: &Tag, path: &Path) -> Result<Written, EmbedError> {
    let mut file = OpenOptions::new().read(true).write(true).open(path)?;
    let size = file.metadata()?.len();
    let room = region(&mut file)?;
    let audio = size.checked_sub(room).ok_or(EmbedError::TagPastEnd)?;
    let need = encode(tag, 0)?.len() as u64;
    let bytes = encode(tag, padding(room, need, audio) as usize)?;
    if bytes.len() as u64 == room {
        file.seek(SeekFrom::Start(0))?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        return Ok(Written::InPlace);
    }
    drop(file);
    atomic_replace(path, |out| {
        out.write_all(&bytes)?;
        let mut src = File::open(path)?;
        src.seek(SeekFrom::Start(room))?;
        let copied = io::copy(&mut src, out)?;
        if copied != audio {
            return Err(io::Error::other(format!(
                "the MP3 changed while it was being rewritten ({copied} of {audio} bytes)"
            )));
        }
        Ok(())
    })?;
    Ok(Written::Rewritten)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_parse_as_far_as_they_go() {
        let d = parse_date("2026-09-28T10:30:05");
        assert_eq!(
            (d.year, d.month, d.day, d.hour, d.minute),
            (Some(2026), Some(9), Some(28), Some(10), Some(30))
        );
        assert_eq!(parse_date("2026").year, Some(2026));
        assert_eq!(parse_date("2026").month, None);
        assert_eq!(parse_date("2026-9").month, None);
        assert_eq!(parse_date("junk"), Date::default());
    }

    #[test]
    fn padding_fills_the_old_room_or_leaves_a_margin() {
        // Fits with little spare: the spare is the padding, so the size is unchanged.
        assert_eq!(padding(5000, 4000, 1_000_000), 1000);
        // Doesn't fit: 1KB + 0.1%.
        assert_eq!(padding(3000, 4000, 1_000_000), 1024 + 1000);
        // Far too much spare (> 10KB + 1%): shrinks to the margin.
        assert_eq!(padding(100_000, 1000, 1_000_000), 1024 + 1000);
    }
}
