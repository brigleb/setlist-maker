//! Loading and saving a set's tracklist: the markdown joined to its JSON sidecar.

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use super::corrections::CorrectionsDb;
use super::fileio::atomic_write;
use super::markdown::{Parsed, parse_markdown_tracklist};
use super::track::Tracklist;

/// The audio a set can be made from, in the order a sibling is looked for. Python searched a
/// `set`, whose order changes from run to run; MP3 is what Ray's sets are.
pub const AUDIO_EXTENSIONS: [&str; 8] = [
    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".aiff",
];

/// The sidecar beside a tracklist: `x_tracklist.md` → `x_tracklist.json`.
pub fn sidecar_path(tracklist_path: &Path) -> PathBuf {
    tracklist_path.with_extension("json")
}

/// Loads a tracklist from its markdown, joining on what the sidecar adds.
///
/// The markdown says which tracks exist; the sidecar carries each track's `coverart_url`,
/// `artwork_pinned` and episode-cover star, matched **by timestamp** (it leaves out rejected
/// rows, so positions don't line up). A sidecar that's missing, unreadable or the wrong shape
/// degrades to the markdown alone. Only the markdown failing to read is an error.
pub fn load_tracklist(tracklist_path: &Path) -> io::Result<Parsed> {
    let sidecar: Option<Value> = fs::read(sidecar_path(tracklist_path))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok());
    let mut parsed = parse_markdown_tracklist(&fs::read_to_string(tracklist_path)?);
    let Some(rows) = sidecar.as_ref().and_then(sidecar_by_timestamp) else {
        return Ok(parsed);
    };
    for track in &mut parsed.tracklist.tracks {
        let Some(row) = rows.get(&track.timestamp) else {
            continue;
        };
        // A string, not merely truthy: a hand-edited number here would reach the artwork
        // cache's key and URL rewriting.
        if let Some(url) = row.get("coverart_url").and_then(Value::as_str)
            && !url.is_empty()
        {
            track.coverart_url = Some(url.to_string());
            track.artwork_pinned = truthy(row.get("artwork_pinned"));
        }
        track.is_episode_cover = truthy(row.get("episode_cover"));
    }
    Ok(parsed)
}

/// The sidecar's rows keyed by timestamp, or `None` where Python's loader would have fallen
/// back to the markdown alone, so a hand-edited file reads the same way in both.
///
/// Python keys a dict by each row's `"timestamp"` and looks tracks up by their integer second,
/// so a key matches when it equals that integer: `180`, `180.0`, and (Python being Python)
/// `true` for 1 and `false` for 0. A later row with an equal key replaces an earlier one. A
/// top level that isn't a list, or a row Python would have raised `TypeError` on, gives no
/// joins at all, as Python's fallback did. A list holding a string with "timestamp" in it, or
/// a list containing that string, is such a row; so is a row whose timestamp is a list or an
/// object (unhashable).
fn sidecar_by_timestamp(sidecar: &Value) -> Option<HashMap<i64, &Map<String, Value>>> {
    let rows = sidecar.as_array()?;
    let mut by_timestamp = HashMap::new();
    for row in rows {
        let row = match row {
            Value::Object(o) => o,
            Value::String(s) if s.contains("timestamp") => return None,
            Value::String(_) => continue,
            Value::Array(a) if a.iter().any(|v| v == "timestamp") => return None,
            Value::Array(_) => continue,
            _ => return None,
        };
        let Some(key) = row.get("timestamp") else {
            continue;
        };
        match key {
            Value::Array(_) | Value::Object(_) => return None,
            _ => {
                if let Some(second) = as_second(key) {
                    by_timestamp.insert(second, row);
                }
            }
        }
    }
    Some(by_timestamp)
}

/// The integer second a JSON value equals under Python's `==`, if any.
fn as_second(v: &Value) -> Option<i64> {
    match v {
        Value::Bool(b) => Some(i64::from(*b)),
        Value::Number(n) => n.as_i64().or_else(|| {
            let f = n.as_f64()?;
            (f.fract() == 0.0 && f >= i64::MIN as f64 && f < i64::MAX as f64).then_some(f as i64)
        }),
        _ => None,
    }
}

/// Python's `bool()` of a JSON value: hand-edited `"false"` is true, `0` and `[]` are false.
pub(crate) fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Writes the tracklist's markdown and sidecar, then the corrections.
///
/// Each file is replaced atomically (#64), and the sidecar goes first: if only one lands, a new
/// sidecar under an old `.md` is harmless (a row it lacks just has no artwork), while a new
/// `.md` over an old sidecar would re-join a corrected track to the stale Shazam URL it was
/// corrected away from (#30).
pub fn save_tracklist(
    tracklist: &Tracklist,
    tracklist_path: &Path,
    corrections: Option<&CorrectionsDb>,
) -> io::Result<()> {
    atomic_write(
        &sidecar_path(tracklist_path),
        tracklist.sidecar_json().as_bytes(),
    )?;
    atomic_write(tracklist_path, tracklist.to_markdown().as_bytes())?;
    if let Some(db) = corrections {
        db.save()?;
    }
    Ok(())
}

/// The audio beside a tracklist: `x.mp3` for `x.md`, else `x.mp3` for `x_tracklist.md`.
pub fn find_audio_file(tracklist_path: &Path) -> Option<PathBuf> {
    let dir = tracklist_path.parent().unwrap_or(Path::new(""));
    let stem = tracklist_path.file_stem()?.to_string_lossy();
    let sibling = |base: &str| {
        AUDIO_EXTENSIONS
            .iter()
            .map(|ext| dir.join(format!("{base}{ext}")))
            .find(|p| p.exists())
    };
    sibling(&stem).or_else(|| sibling(stem.strip_suffix("_tracklist")?))
}

/// The set's audio: the path given, if it still exists, else the sibling of the tracklist.
pub fn resolve_audio_path(audio: Option<&Path>, tracklist_path: &Path) -> Option<PathBuf> {
    match audio {
        Some(a) if a.exists() => Some(a.to_path_buf()),
        _ => find_audio_file(tracklist_path),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Track;
    use serde_json::json;

    const TWO: &str = "# Tracklist: test.mp3\n\n*Generated on 2026-01-31 20:00*\n\n\
                       1. **Artist One** - Track One (0:00)\n2. **Artist Two** - Track Two (3:00)\n";

    fn write_set(dir: &Path, markdown: &str, sidecar: Option<&str>) -> PathBuf {
        let md = dir.join("test_tracklist.md");
        fs::write(&md, markdown).unwrap();
        if let Some(s) = sidecar {
            fs::write(sidecar_path(&md), s).unwrap();
        }
        md
    }

    fn load(md: &Path) -> Tracklist {
        load_tracklist(md).unwrap().tracklist
    }

    fn urls(tl: &Tracklist) -> Vec<Option<&str>> {
        tl.tracks
            .iter()
            .map(|t| t.coverart_url.as_deref())
            .collect()
    }

    #[test]
    fn loads_artwork_urls_from_the_sidecar() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = json!([
            {"timestamp": 0, "time": "0:00", "artist": "Artist One", "title": "Track One",
             "coverart_url": "https://example.com/art1.jpg"},
            {"timestamp": 180, "time": "3:00", "artist": "Artist Two", "title": "Track Two",
             "coverart_url": "https://example.com/art2.jpg"},
        ]);
        let tl = load(&write_set(dir.path(), TWO, Some(&sidecar.to_string())));
        assert_eq!(
            urls(&tl),
            [
                Some("https://example.com/art1.jpg"),
                Some("https://example.com/art2.jpg")
            ]
        );
    }

    #[test]
    fn reopening_recovers_a_fenced_description() {
        let dir = tempfile::tempdir().unwrap();
        let md = "# Tracklist: test.mp3\n\n*Generated on 2026-01-31 20:00*\n\n<!-- summary -->\n\
                  Peaked on 1. **Artist One** - Track One (0:00), a line shaped like a track.\n\
                  <!-- /summary -->\n\n1. **Artist One** - Track One (0:00)\n\
                  2. **Artist Two** - Track Two (3:00)\n";
        let sidecar = json!([
            {"timestamp": 0, "coverart_url": "https://example.com/art1.jpg"},
            {"timestamp": 180, "artist": "Artist Two"},
        ]);
        let tl = load(&write_set(dir.path(), md, Some(&sidecar.to_string())));
        assert_eq!(
            tl.summary.as_deref(),
            Some("Peaked on 1. **Artist One** - Track One (0:00), a line shaped like a track.")
        );
        assert_eq!(urls(&tl), [Some("https://example.com/art1.jpg"), None]);
    }

    #[test]
    fn reopening_keeps_a_row_that_knows_only_its_title() {
        let dir = tempfile::tempdir().unwrap();
        let md = "# Tracklist: test.mp3\n\n*Generated on 2026-01-31 20:00*\n\n\
                  1. **Artist One** - Track One (0:00)\n2. *Unknown artist* - Titled Only (3:00)\n\
                  3. **Artist Two** (6:00)\n";
        let sidecar = json!([
            {"timestamp": 0, "coverart_url": "https://example.com/art1.jpg"},
            {"timestamp": 180, "coverart_url": "https://example.com/art2.jpg"},
            {"timestamp": 360, "coverart_url": "https://example.com/art3.jpg"},
        ]);
        let tl = load(&write_set(dir.path(), md, Some(&sidecar.to_string())));
        let names: Vec<(&str, &str)> = tl
            .tracks
            .iter()
            .map(|t| (t.artist.as_str(), t.title.as_str()))
            .collect();
        assert_eq!(
            names,
            [
                ("Artist One", "Track One"),
                ("", "Titled Only"),
                ("Artist Two", "")
            ]
        );
        assert!(urls(&tl).iter().all(Option::is_some));
    }

    #[test]
    fn matches_by_timestamp_not_index() {
        let dir = tempfile::tempdir().unwrap();
        let md = format!("{TWO}3. **Artist Three** - Track Three (6:00)\n");
        let sidecar = json!([
            {"timestamp": 0, "coverart_url": "https://example.com/art1.jpg"},
            {"timestamp": 360, "coverart_url": "https://example.com/art3.jpg"},
        ]);
        let tl = load(&write_set(dir.path(), &md, Some(&sidecar.to_string())));
        assert_eq!(
            urls(&tl),
            [
                Some("https://example.com/art1.jpg"),
                None,
                Some("https://example.com/art3.jpg")
            ]
        );
    }

    #[test]
    fn a_structurally_wrong_sidecar_degrades_to_the_markdown() {
        for sidecar in [
            "null",
            "42",
            "true",
            r#""a string""#,
            "[1, 2, 3]",
            "{not json",
            r#"{"timestamp": 0}"#,
            r#"[{"timestamp": 0, "coverart_url": "x"}, "has timestamp in it"]"#,
            r#"[{"timestamp": 0, "coverart_url": "x"}, ["timestamp"]]"#,
            r#"[{"timestamp": 0, "coverart_url": "x"}, {"timestamp": [0]}]"#,
            r#"[{"timestamp": 0, "coverart_url": "x"}, null]"#,
        ] {
            let dir = tempfile::tempdir().unwrap();
            let tl = load(&write_set(dir.path(), TWO, Some(sidecar)));
            assert_eq!(tl.tracks.len(), 2, "{sidecar}");
            assert_eq!(urls(&tl), [None, None], "{sidecar}");
        }
    }

    #[test]
    fn rows_python_skipped_are_skipped_not_fatal() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = r#"["a string", [1, 2], {"no": "timestamp"}, {"timestamp": "0"},
                          {"timestamp": 0, "coverart_url": "https://x/1.jpg"}]"#;
        let tl = load(&write_set(dir.path(), TWO, Some(sidecar)));
        assert_eq!(urls(&tl), [Some("https://x/1.jpg"), None]);
    }

    #[test]
    fn a_key_matches_when_python_would_call_it_equal() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = r#"[{"timestamp": 180.0, "coverart_url": "https://x/float.jpg"},
                          {"timestamp": 0.5, "coverart_url": "https://x/half.jpg"}]"#;
        let tl = load(&write_set(dir.path(), TWO, Some(sidecar)));
        assert_eq!(urls(&tl), [None, Some("https://x/float.jpg")]);

        let one = "# Tracklist: t.mp3\n\n*Generated on x*\n\n1. **A** - B (0:01)\n";
        let sidecar = r#"[{"timestamp": true, "coverart_url": "https://x/true.jpg"}]"#;
        let tl = load(&write_set(dir.path(), one, Some(sidecar)));
        assert_eq!(urls(&tl), [Some("https://x/true.jpg")]);
    }

    #[test]
    fn the_last_row_for_a_second_wins() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = r#"[{"timestamp": 0, "coverart_url": "https://x/first.jpg"},
                          {"timestamp": 0, "coverart_url": "https://x/second.jpg"}]"#;
        let tl = load(&write_set(dir.path(), TWO, Some(sidecar)));
        assert_eq!(urls(&tl)[0], Some("https://x/second.jpg"));
    }

    #[test]
    fn falls_back_to_markdown_only_without_a_sidecar() {
        let dir = tempfile::tempdir().unwrap();
        let tl = load(&write_set(dir.path(), TWO, None));
        assert_eq!(urls(&tl), [None, None]);
    }

    #[test]
    fn a_missing_tracklist_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        assert!(load_tracklist(&dir.path().join("nope_tracklist.md")).is_err());
    }

    #[test]
    fn handles_rows_without_coverart_urls() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = r#"[{"timestamp": 0, "time": "0:00", "artist": "A", "title": "B"}]"#;
        let tl = load(&write_set(dir.path(), TWO, Some(sidecar)));
        assert_eq!(urls(&tl), [None, None]);
    }

    #[test]
    fn the_curation_flags_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = json!([
            {"timestamp": 0, "artist": "Artist One"},
            {"timestamp": 180, "coverart_url": "https://itunes/cross.jpg",
             "artwork_pinned": true, "episode_cover": true},
        ]);
        let tl = load(&write_set(dir.path(), TWO, Some(&sidecar.to_string())));
        let covers: Vec<bool> = tl.tracks.iter().map(|t| t.is_episode_cover).collect();
        assert_eq!(covers, [false, true]);
        assert!(tl.tracks[1].artwork_pinned);
        assert_eq!(urls(&tl), [None, Some("https://itunes/cross.jpg")]);
    }

    #[test]
    fn a_sidecar_without_curation_flags_still_loads() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = r#"[{"timestamp": 0, "coverart_url": "https://cdn.shazam.com/a.jpg"}]"#;
        let tl = load(&write_set(dir.path(), TWO, Some(sidecar)));
        assert_eq!(urls(&tl)[0], Some("https://cdn.shazam.com/a.jpg"));
        assert!(!tl.tracks[0].artwork_pinned && !tl.tracks[0].is_episode_cover);
    }

    #[test]
    fn a_non_string_or_empty_coverart_url_is_ignored_and_leaves_the_pin_alone() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = r#"[{"timestamp": 0, "coverart_url": 42, "artwork_pinned": true},
                          {"timestamp": 180, "coverart_url": "", "artwork_pinned": true,
                           "episode_cover": "false"}]"#;
        let tl = load(&write_set(dir.path(), TWO, Some(sidecar)));
        assert_eq!(urls(&tl), [None, None]);
        assert!(!tl.tracks[0].artwork_pinned && !tl.tracks[1].artwork_pinned);
        // Python truthiness: the string "false" is a star.
        assert!(tl.tracks[1].is_episode_cover);
    }

    #[test]
    fn save_writes_markdown_and_sidecar_without_rejected_rows() {
        let dir = tempfile::tempdir().unwrap();
        let tl = Tracklist {
            source_file: "set.mp3".into(),
            generated_on: Some("2026-01-01 00:00".into()),
            tracks: vec![
                Track::new(0, "A", "One"),
                Track {
                    rejected: true,
                    ..Track::new(60, "B", "Two")
                },
            ],
            summary: None,
        };
        let out = dir.path().join("set_tracklist.md");
        save_tracklist(&tl, &out, None).unwrap();
        assert_eq!(fs::read_to_string(&out).unwrap(), tl.to_markdown());
        let written: Value = serde_json::from_str(
            &fs::read_to_string(dir.path().join("set_tracklist.json")).unwrap(),
        )
        .unwrap();
        let artists: Vec<&str> = written
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["artist"].as_str().unwrap())
            .collect();
        assert_eq!(artists, ["A"]);
    }

    #[test]
    fn the_sidecar_is_written_before_the_markdown() {
        // The .md can't be written (a folder is in its way), so only the sidecar lands: the
        // harmless half (#30).
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("set_tracklist.md");
        fs::create_dir(&out).unwrap();
        fs::write(out.join("x"), "").unwrap();
        let tl = Tracklist {
            tracks: vec![Track::new(0, "A", "New")],
            ..Tracklist::default()
        };
        assert!(save_tracklist(&tl, &out, None).is_err());
        let sidecar = fs::read_to_string(dir.path().join("set_tracklist.json")).unwrap();
        assert!(sidecar.contains("\"New\""));
    }

    #[test]
    fn audio_beside_the_tracklist() {
        let dir = tempfile::tempdir().unwrap();
        let md = dir.path().join("set_tracklist.md");
        assert_eq!(resolve_audio_path(None, &md), None);

        let sibling = dir.path().join("set.mp3");
        fs::write(&sibling, "x").unwrap();
        assert_eq!(resolve_audio_path(None, &md), Some(sibling.clone()));

        let given = dir.path().join("given.mp3");
        fs::write(&given, "x").unwrap();
        assert_eq!(resolve_audio_path(Some(&given), &md), Some(given));
        // A moved explicit path falls through to discovery.
        let moved = dir.path().join("moved/set.mp3");
        assert_eq!(resolve_audio_path(Some(&moved), &md), Some(sibling));
    }

    #[test]
    fn an_exact_stem_match_comes_first() {
        let dir = tempfile::tempdir().unwrap();
        let md = dir.path().join("set_tracklist.md");
        fs::write(dir.path().join("set.mp3"), "x").unwrap();
        fs::write(dir.path().join("set_tracklist.wav"), "x").unwrap();
        assert_eq!(
            find_audio_file(&md),
            Some(dir.path().join("set_tracklist.wav"))
        );
    }
}
