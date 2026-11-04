//! The embed against a copy of a 12s fixture MP3 (Logic-like: CBR, no Xing, a v2.3 tag with
//! `TSSE`). Each test reads the bytes back with `raw`, an independent walk of the v2.3 layout,
//! not with the crate that wrote them. From spike 4's tests.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use id3::{Tag, TagLike, Version};

use super::raw;
use super::*;

fn fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("src/audio/fixtures/stereo48_cbr.mp3")
}

fn mp3(dir: &Path) -> PathBuf {
    let out = dir.join("t.mp3");
    fs::copy(fixture(), &out).unwrap();
    out
}

/// The fixture's audio with no tag at all.
fn bare_mp3(dir: &Path) -> PathBuf {
    let bytes = fs::read(fixture()).unwrap();
    let out = dir.join("bare.mp3");
    fs::write(&out, &bytes[raw::tag_len(&bytes)..]).unwrap();
    out
}

/// Chapter art that shrinks per chapter: mutagen sorted frames by size, which put these in
/// exact reverse (#33), so a writer that sorts anything shows up here.
fn chapters(n: u32) -> Vec<Chapter> {
    (0..n)
        .map(|i| Chapter {
            start_ms: i * 1000,
            end_ms: (i + 1) * 1000,
            title: format!("Artist {i} - Title {i}"),
            image: Some(vec![
                0xAB;
                4000usize.saturating_sub(i as usize * 500).max(1)
            ]),
        })
        .collect()
}

fn episode() -> Episode {
    Episode {
        title: Some("September 28, 2026".into()),
        artist: Some("DJ Disarray".into()),
        cover: Some(vec![0xCD; 300]),
    }
}

fn audio_after_tag(bytes: &[u8]) -> &[u8] {
    &bytes[raw::tag_len(bytes)..]
}

fn starts(path: &Path) -> Vec<u32> {
    raw::chapters(&fs::read(path).unwrap())
        .iter()
        .map(|c| c.start_ms)
        .collect()
}

#[test]
fn chapters_sit_on_disk_in_the_order_given() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(6)).unwrap();
    assert_eq!(starts(&f), [0, 1000, 2000, 3000, 4000, 5000]);

    // Nothing sorts behind the caller's back, either way.
    let mut shuffled = chapters(6);
    shuffled.swap(0, 4);
    embed(&f, &episode(), &shuffled).unwrap();
    assert_eq!(starts(&f), [4000, 1000, 2000, 3000, 0, 5000]);
}

#[test]
fn the_tag_is_v23_with_no_flags() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(3)).unwrap();
    let bytes = fs::read(&f).unwrap();
    assert_eq!(&bytes[..6], b"ID3\x03\x00\x00");
    for fr in raw::frames(&bytes) {
        assert_eq!(fr.flags, 0, "{}", fr.id);
    }
}

#[test]
fn each_chapter_has_its_title_and_art_and_the_toc_lists_them() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(3)).unwrap();
    let bytes = fs::read(&f).unwrap();
    let chaps = raw::chapters(&bytes);
    assert_eq!(chaps.len(), 3);
    for (i, c) in chaps.iter().enumerate() {
        assert_eq!(c.element_id, format!("chp{i:03}"));
        assert_eq!(c.end_ms, (i as u32 + 1) * 1000);
        assert_eq!((c.start_offset, c.end_offset), (u32::MAX, u32::MAX));
        let ids: Vec<&str> = c.sub_frames.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["TIT2", "APIC"]);
        // v2.3 sub-frame sizes are plain big-endian (#17): 4000 bytes of art plus its header.
        assert!(c.sub_frames[1].size > 3000);
    }
    let toc = raw::toc(&bytes).expect("CTOC");
    assert_eq!(toc.element_id, "toc");
    assert_eq!(toc.flags, 0x03, "top-level and ordered");
    assert_eq!(toc.children, ["chp000", "chp001", "chp002"]);
    assert_eq!(toc.sub_frames[0].text.as_deref(), Some("Table of Contents"));
}

#[test]
fn a_chapter_with_no_art_gets_no_apic() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let mut cs = chapters(2);
    cs[1].image = None;
    embed(&f, &episode(), &cs).unwrap();
    let chaps = raw::chapters(&fs::read(&f).unwrap());
    assert_eq!(chaps[1].sub_frames.len(), 1);
}

#[test]
fn re_embedding_replaces_the_chapters_and_the_cover() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(6)).unwrap();
    embed(&f, &episode(), &chapters(2)).unwrap();
    let bytes = fs::read(&f).unwrap();
    assert_eq!(raw::chapters(&bytes).len(), 2);
    assert_eq!(raw::toc(&bytes).unwrap().children.len(), 2);
    let top = raw::frames(&bytes);
    assert_eq!(top.iter().filter(|f| f.id == "APIC").count(), 1);
    assert_eq!(top.iter().filter(|f| f.id == "CTOC").count(), 1);
}

#[test]
fn episode_text_is_top_level_and_blank_leaves_the_file_alone() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let tsse = |b: &[u8]| {
        raw::frames(b)
            .into_iter()
            .find(|f| f.id == "TSSE")
            .and_then(|f| f.text)
    };
    let encoder = tsse(&fs::read(&f).unwrap());
    assert!(encoder.is_some());
    embed(&f, &episode(), &chapters(2)).unwrap();
    let blank = Episode {
        title: Some("  ".into()),
        artist: None,
        cover: None,
    };
    embed(&f, &blank, &chapters(2)).unwrap();
    let bytes = fs::read(&f).unwrap();
    let top = raw::frames(&bytes);
    let text = |id: &str| top.iter().find(|f| f.id == id).and_then(|f| f.text.clone());
    assert_eq!(text("TIT2").as_deref(), Some("September 28, 2026"));
    assert_eq!(text("TALB").as_deref(), Some("September 28, 2026"));
    assert_eq!(text("TPE1").as_deref(), Some("DJ Disarray"));
    // No cover given: the one already there stays.
    assert_eq!(top.iter().filter(|f| f.id == "APIC").count(), 1);
    // The recorder's own frame survives every embed.
    assert_eq!(tsse(&bytes), encoder);
    assert_eq!(
        raw::chapters(&bytes)[0].sub_frames[0].text.as_deref(),
        Some("Artist 0 - Title 0")
    );
}

#[test]
fn non_latin1_text_is_utf16_never_utf8_in_v23() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let mut cs = chapters(2);
    cs[0].title = "Altın Gün - Kesik Çayır 🎧".into();
    let ep = Episode {
        title: Some("Set 🎧".into()),
        ..episode()
    };
    embed(&f, &ep, &cs).unwrap();
    let bytes = fs::read(&f).unwrap();
    let tit2 = raw::frames(&bytes)
        .into_iter()
        .find(|f| f.id == "TIT2")
        .unwrap();
    assert_eq!(tit2.encoding, Some(1));
    assert_eq!(tit2.text.as_deref(), Some("Set 🎧"));
    let sub = &raw::chapters(&bytes)[0].sub_frames[0];
    assert_eq!(sub.encoding, Some(1));
    assert_eq!(sub.text.as_deref(), Some("Altın Gün - Kesik Çayır 🎧"));
}

#[test]
fn no_chapters_or_too_many_is_refused_and_the_file_left_alone() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let before = fs::read(&f).unwrap();
    assert!(matches!(
        embed(&f, &episode(), &[]),
        Err(EmbedError::NoChapters)
    ));
    // CTOC's child count is one byte: 256 would be a table listing none of them.
    let mut cs = chapters(256);
    for c in &mut cs {
        c.image = None;
    }
    assert!(matches!(
        embed(&f, &episode(), &cs),
        Err(EmbedError::TooManyChapters(256))
    ));
    assert_eq!(fs::read(&f).unwrap(), before);
    cs.pop();
    embed(&f, &episode(), &cs).unwrap();
    assert_eq!(
        raw::toc(&fs::read(&f).unwrap()).unwrap().children.len(),
        255
    );
}

/// A smaller tag is padded out to the room the old one had, as mutagen does, so the audio
/// stays where it is.
#[test]
fn a_re_embed_that_fits_is_written_in_place() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(6)).unwrap();
    let first = fs::read(&f).unwrap();
    let ino = fs::metadata(&f).unwrap();
    assert_eq!(
        embed(&f, &episode(), &chapters(2)).unwrap(),
        Written::InPlace
    );
    let second = fs::read(&f).unwrap();
    assert_eq!(raw::tag_len(&second), raw::tag_len(&first));
    assert_eq!(second.len(), first.len());
    assert_eq!(audio_after_tag(&second), audio_after_tag(&first));
    assert_eq!(raw::chapters(&second).len(), 2);
    // Written into the same file, not a replacement.
    use std::os::unix::fs::MetadataExt;
    assert_eq!(fs::metadata(&f).unwrap().ino(), ino.ino());
}

/// A tag that outgrows its room is written to a new file beside the old one with the audio
/// copied after it, then renamed over: the same audio bytes, with 1KB + 0.1% of padding so the
/// next small change fits in place.
#[test]
fn a_tag_that_grows_rewrites_the_file_and_leaves_room() {
    let dir = tempfile::tempdir().unwrap();
    let f = bare_mp3(dir.path());
    fs::set_permissions(&f, fs::Permissions::from_mode(0o640)).unwrap();
    let before = fs::read(&f).unwrap();
    assert_eq!(
        embed(&f, &episode(), &chapters(3)).unwrap(),
        Written::Rewritten
    );
    let first = fs::read(&f).unwrap();
    assert_eq!(audio_after_tag(&first), &before[..]);
    assert_eq!(raw::padding(&first), 1024 + before.len() / 1000);
    assert_eq!(
        fs::metadata(&f).unwrap().permissions().mode() & 0o777,
        0o640
    );
    // Nothing left beside it.
    assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 1);

    let bigger = Episode {
        cover: Some(vec![0xCD; 900]),
        ..episode()
    };
    assert_eq!(embed(&f, &bigger, &chapters(3)).unwrap(), Written::InPlace);
    let second = fs::read(&f).unwrap();
    assert_eq!(raw::tag_len(&second), raw::tag_len(&first));
    assert_eq!(audio_after_tag(&second), &before[..]);
}

#[test]
fn a_symlinked_mp3_stays_a_symlink() {
    let dir = tempfile::tempdir().unwrap();
    let real = bare_mp3(dir.path());
    let link = dir.path().join("link.mp3");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    assert_eq!(
        embed(&link, &episode(), &chapters(2)).unwrap(),
        Written::Rewritten
    );
    assert!(
        fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert_eq!(raw::chapters(&fs::read(&real).unwrap()).len(), 2);
}

fn syncsafe(n: usize) -> [u8; 4] {
    [21, 14, 7, 0].map(|shift| ((n >> shift) & 0x7f) as u8)
}

fn v24_frame(id: &str, data: &[u8]) -> Vec<u8> {
    [id.as_bytes(), &syncsafe(data.len()), &[0, 0], data].concat()
}

/// A tag written elsewhere as v2.4 comes out as mutagen's `update_to_v23` would leave it.
#[test]
fn a_v24_tag_is_converted() {
    let dir = tempfile::tempdir().unwrap();
    let bare = fs::read(bare_mp3(dir.path())).unwrap();
    // By hand, as ffmpeg writes it: UTF-8 text ending in the NUL v2.4 terminates values with
    // (the crate's own v2.4 writer leaves it off, so it can't make this file).
    let frames: Vec<u8> = [
        v24_frame("TIT2", b"\x03x\x00"),
        v24_frame("TDRC", b"\x032026-09-28T10:30\x00"),
        v24_frame("TSOP", b"\x03Disarray, DJ\x00"),
        v24_frame("TXXX", "\x03note\x00Café 🎧\x00".as_bytes()),
    ]
    .concat();
    let mut bytes = b"ID3\x04\x00\x00".to_vec();
    bytes.extend(syncsafe(frames.len()));
    bytes.extend(frames);
    bytes.extend_from_slice(&bare);
    let f = dir.path().join("v24.mp3");
    fs::write(&f, &bytes).unwrap();

    let keep_text = Episode {
        cover: None,
        ..Episode::default()
    };
    embed(&f, &keep_text, &chapters(2)).unwrap();
    let out = fs::read(&f).unwrap();
    let top = raw::frames(&out);
    let text = |id: &str| top.iter().find(|f| f.id == id).and_then(|f| f.text.clone());
    assert_eq!(text("TIT2").as_deref(), Some("x"), "no trailing NUL as '/'");
    assert_eq!(text("TYER").as_deref(), Some("2026"));
    assert_eq!(text("TDAT").as_deref(), Some("2809"));
    assert_eq!(text("TIME").as_deref(), Some("1030"));
    for gone in ["TDRC", "TSOP"] {
        assert!(top.iter().all(|f| f.id != gone), "{gone} is v2.4-only");
    }
    for fr in &top {
        assert_ne!(fr.encoding, Some(3), "{} is UTF-8 in a v2.3 tag", fr.id);
    }
    assert_eq!(audio_after_tag(&out), &bare[..]);
}

/// A file cut short (an iCloud placeholder mid-download) can declare a tag longer than the
/// file. That is an error, not a panic, and the file is left alone.
#[test]
fn a_tag_longer_than_the_file_is_an_error() {
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join("cut.mp3");
    let mut bytes = b"ID3\x03\x00\x00\x00\x00\x7f\x7f".to_vec();
    bytes.extend_from_slice(b"TIT2\x00\x00\x00\x02\x00\x00\x00a");
    fs::write(&f, &bytes).unwrap();
    assert!(embed(&f, &episode(), &chapters(1)).is_err());
    assert_eq!(fs::read(&f).unwrap(), bytes);
}

/// ffprobe is how hosts and web players read chapters (#33). Skipped without it, unless `CI`.
#[test]
fn ffprobe_lists_the_chapters_in_time_order() {
    if Command::new("ffprobe").arg("-version").output().is_err() {
        assert!(
            std::env::var_os("CI").is_none(),
            "ffprobe is required in CI"
        );
        eprintln!("skipped: no ffprobe");
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let mut cs = chapters(6);
    cs[3].title = "Café 🎧".into();
    embed(&f, &episode(), &cs).unwrap();
    let out = Command::new("ffprobe")
        .args(["-v", "error", "-show_chapters", "-of", "json"])
        .arg(&f)
        .output()
        .unwrap();
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let got: Vec<(i64, String)> = v["chapters"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            (
                c["start"].as_i64().unwrap(),
                c["tags"]["title"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let want: Vec<(i64, String)> = cs
        .iter()
        .map(|c| (c.start_ms as i64, c.title.clone()))
        .collect();
    assert_eq!(got, want);
}

#[test]
fn the_reader_and_the_crate_agree_on_what_was_written() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(2)).unwrap();
    let tag = Tag::read_from_path(&f).unwrap();
    assert_eq!(tag.version(), Version::Id3v23);
    assert_eq!(tag.chapters().count(), 2);
    assert_eq!(tag.title(), Some("September 28, 2026"));
}
