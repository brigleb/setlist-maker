//! `id3-spike <fixture-dir> <clone.mp3>`: embed the fixture `dump_fixture.py` wrote (A/B side B)
//! and report the time, the tag size before and after, and whether the audio moved.

use std::path::{Path, PathBuf};
use std::time::Instant;

use id3_spike::{embed, raw, Chapter, Episode};
use serde_json::Value;

fn read(dir: &Path, name: &str) -> Vec<u8> {
    std::fs::read(dir.join(name)).unwrap_or_else(|e| panic!("{name}: {e}"))
}

fn head(path: &Path) -> (u64, usize) {
    use std::io::Read;
    let mut b = vec![0; 10];
    std::fs::File::open(path)
        .unwrap()
        .read_exact(&mut b)
        .unwrap();
    (std::fs::metadata(path).unwrap().len(), raw::tag_len(&b))
}

fn main() {
    let args: Vec<PathBuf> = std::env::args_os().skip(1).map(PathBuf::from).collect();
    let [dir, mp3] = &args[..] else {
        panic!("usage: id3-spike <fixture-dir> <clone.mp3>");
    };
    let rows: Vec<Value> = serde_json::from_slice(&read(dir, "chapters.json")).unwrap();
    let chapters: Vec<Chapter> = rows
        .iter()
        .map(|r| Chapter {
            image: r["image"].as_str().map(|n| read(dir, n)),
            ..serde_json::from_value(r.clone()).unwrap()
        })
        .collect();
    let ep: Value = serde_json::from_slice(&read(dir, "episode.json")).unwrap();
    let episode = Episode {
        title: ep["title"].as_str().map(String::from),
        artist: ep["artist"].as_str().map(String::from),
        cover: ep["cover"].as_str().map(|n| read(dir, n)),
    };

    let (size0, tag0) = head(mp3);
    let t0 = Instant::now();
    embed(mp3, &episode, &chapters).unwrap();
    let ms = t0.elapsed().as_millis();
    let (size1, tag1) = head(mp3);
    println!(
        "rust embed: {ms}ms; tag {tag0} -> {tag1} bytes; file {size0} -> {size1}; audio {}",
        if tag0 == tag1 { "stayed put" } else { "moved" }
    );
}
