//! Spike 4 against a 6s MP3 that ffmpeg makes per test. Each test reads the bytes back with
//! `raw::frames`, an independent walk of the v2.3 layout, not with the crate that wrote them.

use std::path::{Path, PathBuf};
use std::process::Command;

use id3_spike::{embed, raw, Chapter, Episode};

fn mp3(dir: &Path) -> PathBuf {
    mp3_with(dir, &["-id3v2_version", "3"])
}

fn mp3_with(dir: &Path, tag_args: &[&str]) -> PathBuf {
    let out = dir.join("t.mp3");
    let ok = Command::new("ffmpeg")
        .args([
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
        ])
        .args(["-c:a", "libmp3lame", "-b:a", "64k"])
        .args(tag_args)
        .arg("-y")
        .arg(&out)
        .status()
        .expect("ffmpeg")
        .success();
    assert!(ok);
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

#[test]
fn chapters_sit_on_disk_in_the_order_given() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(6)).unwrap();
    let starts: Vec<u32> = raw::chapters(&std::fs::read(&f).unwrap())
        .iter()
        .map(|c| c.start_ms)
        .collect();
    assert_eq!(starts, [0, 1000, 2000, 3000, 4000, 5000]);
}

#[test]
fn the_crate_keeps_insertion_order_not_time_order() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let mut shuffled = chapters(6);
    shuffled.swap(0, 4);
    shuffled.swap(1, 5);
    embed(&f, &episode(), &shuffled).unwrap();
    let starts: Vec<u32> = raw::chapters(&std::fs::read(&f).unwrap())
        .iter()
        .map(|c| c.start_ms)
        .collect();
    assert_eq!(starts, [4000, 5000, 2000, 3000, 0, 1000]);
}

#[test]
fn the_tag_is_v23_with_no_flags() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(3)).unwrap();
    assert_eq!(&std::fs::read(&f).unwrap()[..6], b"ID3\x03\x00\x00");
}

#[test]
fn each_chapter_has_its_title_and_art_and_the_toc_lists_them() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(3)).unwrap();
    let bytes = std::fs::read(&f).unwrap();
    let chaps = raw::chapters(&bytes);
    assert_eq!(chaps.len(), 3);
    for (i, c) in chaps.iter().enumerate() {
        assert_eq!(c.element_id, format!("chp{i:03}"));
        assert_eq!(c.end_ms, (i as u32 + 1) * 1000);
        let ids: Vec<&str> = c.sub_frames.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["TIT2", "APIC"]);
        // v2.3 sub-frame sizes are plain big-endian (#17): 4000 bytes of art plus its header.
        assert!(c.sub_frames[1].size > 3000);
    }
    let toc = raw::toc(&bytes).expect("CTOC");
    assert_eq!(toc.flags, 0x03, "top-level and ordered");
    assert_eq!(toc.children, ["chp000", "chp001", "chp002"]);
}

#[test]
fn a_chapter_with_no_art_gets_no_apic() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let mut cs = chapters(2);
    cs[1].image = None;
    embed(&f, &episode(), &cs).unwrap();
    let chaps = raw::chapters(&std::fs::read(&f).unwrap());
    assert_eq!(chaps[1].sub_frames.len(), 1);
}

#[test]
fn re_embedding_replaces_the_chapters_and_the_cover() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(6)).unwrap();
    embed(&f, &episode(), &chapters(2)).unwrap();
    let bytes = std::fs::read(&f).unwrap();
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
    let encoder = tsse(&std::fs::read(&f).unwrap());
    assert!(encoder.is_some());
    embed(&f, &episode(), &chapters(2)).unwrap();
    let blank = Episode {
        title: Some("  ".into()),
        artist: None,
        cover: None,
    };
    embed(&f, &blank, &chapters(2)).unwrap();
    let bytes = std::fs::read(&f).unwrap();
    let top = raw::frames(&bytes);
    let text = |id: &str| top.iter().find(|f| f.id == id).and_then(|f| f.text.clone());
    assert_eq!(text("TIT2").as_deref(), Some("September 28, 2026"));
    assert_eq!(text("TALB").as_deref(), Some("September 28, 2026"));
    assert_eq!(text("TPE1").as_deref(), Some("DJ Disarray"));
    // No cover given: the one already there stays.
    assert_eq!(top.iter().filter(|f| f.id == "APIC").count(), 1);
    // ffmpeg's own frame survives every embed.
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
    cs[0].title = "Café 🎧 - Ünïcode".into();
    let ep = Episode {
        title: Some("Set 🎧".into()),
        ..episode()
    };
    embed(&f, &ep, &cs).unwrap();
    let bytes = std::fs::read(&f).unwrap();
    let tit2 = raw::frames(&bytes)
        .into_iter()
        .find(|f| f.id == "TIT2")
        .unwrap();
    assert_eq!(tit2.encoding, Some(1));
    assert_eq!(tit2.text.as_deref(), Some("Set 🎧"));
    let sub = &raw::chapters(&bytes)[0].sub_frames[0];
    assert_eq!(sub.encoding, Some(1));
    assert_eq!(sub.text.as_deref(), Some("Café 🎧 - Ünïcode"));
}

#[test]
fn the_audio_after_the_tag_is_untouched() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let before = std::fs::read(&f).unwrap();
    embed(&f, &episode(), &chapters(6)).unwrap();
    embed(&f, &episode(), &chapters(1)).unwrap();
    let after = std::fs::read(&f).unwrap();
    assert_eq!(audio_after_tag(&before), audio_after_tag(&after));
}

#[test]
fn ffprobe_lists_the_chapters_in_time_order() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let mut cs = chapters(6);
    cs[3].title = "Café 🎧".into();
    embed(&f, &episode(), &cs).unwrap();
    let out = Command::new("ffprobe")
        .args(["-v", "error", "-show_chapters", "-of", "json"])
        .arg(&f)
        .output()
        .expect("ffprobe");
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

/// CTOC's child count is one byte, and the crate writes `len() as u8`: 256 chapters would be a
/// table listing none of them.
#[test]
fn more_than_255_chapters_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    let before = std::fs::read(&f).unwrap();
    let mut cs = chapters(256);
    for c in &mut cs {
        c.image = None;
    }
    assert!(embed(&f, &episode(), &cs).is_err());
    assert_eq!(std::fs::read(&f).unwrap(), before);
    cs.pop();
    embed(&f, &episode(), &cs).unwrap();
    assert_eq!(
        raw::toc(&std::fs::read(&f).unwrap())
            .unwrap()
            .children
            .len(),
        255
    );
}

/// A smaller tag is padded out to the room the old one had, as mutagen does, rather than
/// pulling every byte of audio forward (the crate's own `write_to_path` writes no padding).
#[test]
fn a_re_embed_that_fits_leaves_the_audio_where_it_is() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(6)).unwrap();
    let first = std::fs::read(&f).unwrap();
    embed(&f, &episode(), &chapters(2)).unwrap();
    let second = std::fs::read(&f).unwrap();
    assert_eq!(raw::tag_len(&second), raw::tag_len(&first));
    assert_eq!(second.len(), first.len());
    assert_eq!(audio_after_tag(&second), audio_after_tag(&first));
    assert_eq!(raw::chapters(&second).len(), 2);
}

/// When the tag has to grow, it grows with room to spare, so the next small change (a
/// slightly bigger cover) fits without moving the audio again.
#[test]
fn a_tag_that_grows_leaves_room_for_the_next_one() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3(dir.path());
    embed(&f, &episode(), &chapters(3)).unwrap();
    let first = std::fs::read(&f).unwrap();
    assert!(
        raw::padding(&first) >= 1024,
        "padding {}",
        raw::padding(&first)
    );
    let bigger = Episode {
        cover: Some(vec![0xCD; 900]),
        ..episode()
    };
    embed(&f, &bigger, &chapters(3)).unwrap();
    let second = std::fs::read(&f).unwrap();
    assert_eq!(raw::tag_len(&second), raw::tag_len(&first));
    assert_eq!(audio_after_tag(&second), audio_after_tag(&first));
}

/// A recorder's v2.4 tag can hold UTF-8 text, which v2.3 doesn't have; the frames carried
/// over must come out as UTF-16.
#[test]
fn a_v24_file_comes_out_v23_with_no_utf8() {
    let dir = tempfile::tempdir().unwrap();
    let f = mp3_with(
        dir.path(),
        &["-id3v2_version", "4", "-metadata", "comment=Café 🎧"],
    );
    assert_eq!(std::fs::read(&f).unwrap()[3], 4);
    embed(&f, &episode(), &chapters(2)).unwrap();
    let bytes = std::fs::read(&f).unwrap();
    for fr in raw::frames(&bytes) {
        assert_ne!(fr.encoding, Some(3), "{} is UTF-8 in a v2.3 tag", fr.id);
    }
}

/// A file cut short (an iCloud placeholder mid-download) can declare a tag longer than the file.
/// That is an error, not a panic, and the file is left alone.
#[test]
fn a_tag_longer_than_the_file_is_an_error() {
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join("cut.mp3");
    let mut bytes = b"ID3\x03\x00\x00\x00\x00\x7f\x7f".to_vec();
    bytes.extend_from_slice(b"TIT2\x00\x00\x00\x02\x00\x00\x00a");
    std::fs::write(&f, &bytes).unwrap();
    assert!(embed(&f, &episode(), &chapters(1)).is_err());
    assert_eq!(std::fs::read(&f).unwrap(), bytes);
}
