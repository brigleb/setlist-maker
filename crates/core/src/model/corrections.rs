//! Corrections: every artist/title change the DJ ever made, applied to later runs.
//!
//! Stored as `{"corrections": {"<artist>|||<title>": {...}}}` in `corrections.json` under the
//! config folder. It is every correction ever learned, so loading it must never risk it (#64).

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};

use super::fileio::{atomic_write, config_dir};
use super::track::Tracklist;

pub struct CorrectionsDb {
    path: PathBuf,
    /// Keyed by [`key`]; values kept as loaded so a re-save writes back what it read.
    corrections: Map<String, Value>,
    /// The file exists but couldn't be read: saving would replace every correction ever
    /// learned with only this session's.
    unsaveable: bool,
}

/// The lookup key: lowercased, then stripped, as Python's `_make_key` builds it. It is
/// persisted, so it must stay byte-for-byte what Python wrote.
pub fn key(artist: &str, title: &str) -> String {
    format!(
        "{}|||{}",
        artist.to_lowercase().trim(),
        title.to_lowercase().trim()
    )
}

impl CorrectionsDb {
    /// `corrections.json` in the config folder.
    pub fn open_default() -> Self {
        Self::open(config_dir().join("corrections.json"))
    }

    /// Loads corrections, never at the cost of the file's contents (#64).
    ///
    /// Missing: empty. Unparseable or the wrong shape (a half-synced iCloud copy, a stray hand
    /// edit): moved aside to `corrections.json.corrupt-<time>`, and this session starts empty,
    /// so the next save can't overwrite it. Unreadable (permissions, I/O): it can't be moved
    /// either, so saving is refused for the session instead. Invalid UTF-8 counts as
    /// unparseable; Python crashed on it.
    pub fn open(path: impl Into<PathBuf>) -> Self {
        let mut db = CorrectionsDb {
            path: path.into(),
            corrections: Map::new(),
            unsaveable: false,
        };
        let raw = match fs::read(&db.path) {
            Ok(raw) => raw,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return db,
            Err(e) => {
                db.unsaveable = true;
                log::warn!(
                    "could not read corrections from {} ({e}); corrections from this session \
                     will not be saved",
                    db.path.display()
                );
                return db;
            }
        };
        match parse(&raw) {
            Ok(corrections) => db.corrections = corrections,
            Err(why) => db.quarantine(&why),
        }
        db
    }

    fn quarantine(&mut self, why: &str) {
        let stamp = chrono::Local::now().format("%Y%m%d-%H%M%S");
        let name = self.path.file_name().unwrap_or_default().to_string_lossy();
        let aside = self.path.with_file_name(format!("{name}.corrupt-{stamp}"));
        match fs::rename(&self.path, &aside) {
            Ok(()) => log::warn!(
                "corrections file {} is unreadable ({why}); kept it as {} and started a new one",
                self.path.display(),
                aside.display()
            ),
            Err(e) => {
                self.unsaveable = true;
                log::warn!(
                    "corrections file {} is unreadable ({why}) and could not be moved aside \
                     ({e}); corrections from this session will not be saved",
                    self.path.display()
                );
            }
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The stored corrections, keyed by [`key`], as the file holds them.
    pub fn corrections(&self) -> &Map<String, Value> {
        &self.corrections
    }

    /// Writes the corrections atomically. Refused (with a warning, not an error) when the file
    /// couldn't be read at load.
    pub fn save(&self) -> io::Result<()> {
        if self.unsaveable {
            log::warn!(
                "not saving corrections: {} could not be read",
                self.path.display()
            );
            return Ok(());
        }
        let file = json!({ "corrections": self.corrections });
        let text = serde_json::to_string_pretty(&file).expect("a JSON value always serializes");
        atomic_write(&self.path, text.as_bytes())
    }

    pub fn add_correction(
        &mut self,
        original_artist: &str,
        original_title: &str,
        corrected_artist: &str,
        corrected_title: &str,
    ) {
        self.corrections.insert(
            key(original_artist, original_title),
            json!({
                "artist": corrected_artist,
                "title": corrected_title,
                "original_artist": original_artist,
                "original_title": original_title,
                "corrected_at": iso_now(),
            }),
        );
    }

    /// The corrected `(artist, title)` for what Shazam said, if one was learned. A hand-edited
    /// entry without string names is ignored (Python raised on it).
    pub fn get_correction(&self, artist: &str, title: &str) -> Option<(String, String)> {
        let entry = self.corrections.get(&key(artist, title))?;
        Some((
            entry.get("artist")?.as_str()?.to_string(),
            entry.get("title")?.as_str()?.to_string(),
        ))
    }

    /// Applies every known correction to the tracklist and returns how many it applied.
    /// Unidentified tracks are skipped: there's nothing to look up.
    pub fn apply_corrections(&self, tracklist: &mut Tracklist) -> usize {
        let mut applied = 0;
        for track in &mut tracklist.tracks {
            if track.is_unidentified() {
                continue;
            }
            if let Some((artist, title)) = self.get_correction(&track.artist, &track.title) {
                track.original_artist = Some(std::mem::replace(&mut track.artist, artist));
                track.original_title = Some(std::mem::replace(&mut track.title, title));
                applied += 1;
            }
        }
        applied
    }
}

fn parse(raw: &[u8]) -> Result<Map<String, Value>, String> {
    let text = std::str::from_utf8(raw).map_err(|e| e.to_string())?;
    let data: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let Value::Object(mut data) = data else {
        return Err("expected an object with a 'corrections' object".into());
    };
    match data.remove("corrections") {
        None => Ok(Map::new()),
        Some(Value::Object(c)) => Ok(c),
        Some(_) => Err("expected an object with a 'corrections' object".into()),
    }
}

/// Python's `datetime.now().isoformat()`: local time, microseconds only when non-zero.
fn iso_now() -> String {
    let now = chrono::Local::now().naive_local();
    if now.and_utc().timestamp_subsec_micros() == 0 {
        now.format("%Y-%m-%dT%H:%M:%S").to_string()
    } else {
        now.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Track;
    use std::os::unix::fs::PermissionsExt;

    fn sample() -> Tracklist {
        Tracklist {
            tracks: vec![
                Track::new(0, "Daft Punk", "Around the World"),
                Track::new(180, "The Chemical Brothers", "Block Rockin' Beats"),
                Track::new(360, "", ""),
                Track::new(540, "Fatboy Slim", "Praise You"),
            ],
            ..Tracklist::default()
        }
    }

    fn fresh() -> (tempfile::TempDir, CorrectionsDb) {
        let dir = tempfile::tempdir().unwrap();
        let db = CorrectionsDb::open(dir.path().join("corrections.json"));
        (dir, db)
    }

    fn corrupt_copies(dir: &Path) -> Vec<PathBuf> {
        fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .filter(|p| p.to_string_lossy().contains(".corrupt-"))
            .collect()
    }

    #[test]
    fn add_and_get() {
        let (_dir, mut db) = fresh();
        db.add_correction("Orig Artist", "Orig Title", "Fixed Artist", "Fixed Title");
        assert_eq!(
            db.get_correction("Orig Artist", "Orig Title"),
            Some(("Fixed Artist".into(), "Fixed Title".into()))
        );
        assert_eq!(db.get_correction("Unknown", "Track"), None);
    }

    #[test]
    fn lookups_ignore_case() {
        let (_dir, mut db) = fresh();
        db.add_correction("Artist Name", "Track Title", "Fixed", "Fixed");
        assert_eq!(
            db.get_correction("ARTIST NAME", "TRACK TITLE"),
            Some(("Fixed".into(), "Fixed".into()))
        );
    }

    #[test]
    fn the_key_is_lowered_then_stripped() {
        assert_eq!(key("  Daft Punk ", " ONE More "), "daft punk|||one more");
    }

    #[test]
    fn save_and_load() {
        let (dir, mut db) = fresh();
        db.add_correction("Orig", "Title", "Fixed", "Title");
        db.save().unwrap();
        let again = CorrectionsDb::open(dir.path().join("corrections.json"));
        assert_eq!(
            again.get_correction("Orig", "Title"),
            Some(("Fixed".into(), "Title".into()))
        );
        let entry = &again.corrections()["orig|||title"];
        assert_eq!(entry["original_artist"], "Orig");
        assert!(entry["corrected_at"].as_str().unwrap().contains('T'));
    }

    #[test]
    fn a_resave_keeps_what_it_loaded_in_order() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("corrections.json");
        fs::write(
            &path,
            r#"{"other": 1, "corrections": {"z|||z": {"artist": "Z", "title": "Z", "extra": [1]},
                "a|||a": {"artist": "A", "title": "A"}}}"#,
        )
        .unwrap();
        let mut db = CorrectionsDb::open(&path);
        db.add_correction("z", "z", "Zed", "Zed");
        db.add_correction("m", "m", "M", "M");
        db.save().unwrap();
        let saved: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        let keys: Vec<&String> = saved["corrections"].as_object().unwrap().keys().collect();
        // Replacing an entry keeps its place, as a Python dict does; only `corrections` is
        // written back, as Python's save did.
        assert_eq!(keys, ["z|||z", "a|||a", "m|||m"]);
        assert_eq!(saved.as_object().unwrap().len(), 1);
    }

    #[test]
    fn apply_corrections() {
        let (_dir, mut db) = fresh();
        db.add_correction(
            "Daft Punk",
            "Around the World",
            "Daft Punk",
            "Around the World (Album Version)",
        );
        let mut tl = sample();
        assert_eq!(db.apply_corrections(&mut tl), 1);
        assert_eq!(tl.tracks[0].title, "Around the World (Album Version)");
        assert_eq!(
            tl.tracks[0].original_title.as_deref(),
            Some("Around the World")
        );
    }

    #[test]
    fn apply_corrections_skips_unidentified() {
        let (_dir, mut db) = fresh();
        db.add_correction("", "", "Should", "Not Match");
        let mut tl = sample();
        db.apply_corrections(&mut tl);
        assert!(tl.tracks[2].is_unidentified());
    }

    #[test]
    fn missing_or_empty_files_are_just_empty() {
        let (dir, db) = fresh();
        assert!(db.corrections().is_empty());
        for empty in ["{}", r#"{"corrections": {}}"#] {
            let path = dir.path().join("corrections.json");
            fs::write(&path, empty).unwrap();
            assert!(CorrectionsDb::open(&path).corrections().is_empty());
        }
        assert!(corrupt_copies(dir.path()).is_empty());
    }

    #[test]
    fn a_good_file_loads() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("corrections.json");
        fs::write(
            &path,
            r#"{"corrections": {"a|||b": {"artist": "A", "title": "B"}}}"#,
        )
        .unwrap();
        let db = CorrectionsDb::open(&path);
        assert_eq!(
            Value::Object(db.corrections().clone()),
            json!({"a|||b": {"artist": "A", "title": "B"}})
        );
    }

    #[test]
    fn a_corrupt_file_is_kept_not_wiped() {
        for junk in [
            "{not json",
            "[1, 2, 3]",
            r#"{"corrections": []}"#,
            "",
            "\u{0}\u{ff}",
        ] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("corrections.json");
            fs::write(&path, junk.as_bytes()).unwrap();
            let mut db = CorrectionsDb::open(&path);
            assert!(db.corrections().is_empty(), "{junk:?}");
            db.add_correction("Wrong", "Name", "Right", "Name");
            db.save().unwrap();

            let kept = corrupt_copies(dir.path());
            assert_eq!(kept.len(), 1, "{junk:?}");
            assert_eq!(fs::read(&kept[0]).unwrap(), junk.as_bytes());
            let saved: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
            let keys: Vec<&String> = saved["corrections"].as_object().unwrap().keys().collect();
            assert_eq!(keys, ["wrong|||name"]);
        }
    }

    #[test]
    fn invalid_utf8_is_corrupt() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("corrections.json");
        fs::write(&path, [b'{', 0xff, b'}']).unwrap();
        CorrectionsDb::open(&path);
        assert_eq!(corrupt_copies(dir.path()).len(), 1);
    }

    #[test]
    fn an_unreadable_file_is_never_overwritten() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("corrections.json");
        let original = r#"{"corrections": {"a|||b": {"artist": "A", "title": "B"}}}"#;
        fs::write(&path, original).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();
        let mut db = CorrectionsDb::open(&path);
        db.add_correction("X", "Y", "Z", "W");
        db.save().unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), original);
        assert!(corrupt_copies(dir.path()).is_empty());
    }

    #[test]
    fn opening_creates_nothing_and_saving_creates_the_folder() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("hindsight/corrections.json");
        let db = CorrectionsDb::open(&path);
        assert!(!path.parent().unwrap().exists());
        db.save().unwrap();
        assert!(path.exists());
    }

    #[test]
    fn a_malformed_entry_is_no_correction() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("corrections.json");
        fs::write(
            &path,
            r#"{"corrections": {"a|||b": {"artist": 1, "title": "B"}}}"#,
        )
        .unwrap();
        assert_eq!(CorrectionsDb::open(&path).get_correction("a", "b"), None);
    }
}
