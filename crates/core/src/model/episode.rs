//! The episode's own title and artist, written into the MP3 beside its chapters.
//!
//! Stored per set in `<set>_episode.json` beside the tracklist: a sibling file, like
//! `<set>_cover.jpg`, because the sidecar is a bare list of tracks and must stay one. A set
//! with no file gets defaults: the title is the date its name starts with
//! (`2026-09-23-Keys-Lounge` → "September 23, 2026") and the artist is the last one saved,
//! remembered in the config folder's `config.json`. A saved blank means "leave that tag
//! alone", since the recorder may already have tagged the file.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use chrono::{Datelike, NaiveDate};
use regex::Regex;
use serde_json::{Map, Value};

use super::fileio::{atomic_write, config_dir};
use super::uploads::set_stem;

/// One line in a podcast app's header; nothing longer is a title.
pub const MAX_TAG_LENGTH: usize = 200;

/// The set's title and artist. Empty means "write nothing".
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct EpisodeTags {
    pub title: String,
    pub artist: String,
}

/// Where this set's episode title and artist live (may not exist).
pub fn episode_path_for(tracklist_path: &Path) -> PathBuf {
    tracklist_path.with_file_name(format!("{}_episode.json", set_stem(tracklist_path)))
}

/// The user-level settings file in a config folder.
pub fn config_path_in(config_dir: &Path) -> PathBuf {
    config_dir.join("config.json")
}

/// One line, trimmed and capped at [`MAX_TAG_LENGTH`] characters; anything that isn't a
/// string is blank.
pub fn clean_tag(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(s)) => clean_str(s),
        _ => String::new(),
    }
}

fn clean_str(s: &str) -> String {
    let one_line = s.split_whitespace().collect::<Vec<_>>().join(" ");
    one_line.chars().take(MAX_TAG_LENGTH).collect()
}

/// `^(\d{4})-(\d{2})-(\d{2})(?!\d)`, the lookahead done by hand (the regex crate has none).
static DATE_PREFIX: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})").unwrap());
static DIGIT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d").unwrap());

/// The date a set's name starts with, as "September 23, 2026"; else blank.
pub fn default_title(tracklist_path: &Path) -> String {
    let stem = set_stem(tracklist_path);
    let Some(m) = DATE_PREFIX.captures(&stem) else {
        return String::new();
    };
    if DIGIT.is_match(&stem[m[0].len()..]) {
        return String::new();
    }
    let date = NaiveDate::from_ymd_opt(
        m[1].parse().unwrap_or(0),
        m[2].parse().unwrap_or(0),
        m[3].parse().unwrap_or(0),
    );
    // Python's `date` has no year 0, and writes the year unpadded (`999`, not `0999`).
    date.filter(|d| d.year() >= 1)
        .map(|d| format!("{} {}, {}", d.format("%B"), d.day(), d.year()))
        .unwrap_or_default()
}

/// A JSON object from a file: missing, unreadable, unparseable or not an object is empty.
fn read_object(path: &Path) -> Map<String, Value> {
    let raw = match fs::read(path) {
        Ok(raw) => raw,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Map::new(),
        Err(e) => {
            log::warn!("ignoring unreadable {}: {e}", path.display());
            return Map::new();
        }
    };
    match serde_json::from_slice(&raw) {
        Ok(Value::Object(o)) => o,
        Ok(_) => Map::new(),
        Err(e) => {
            log::warn!("ignoring unreadable {}: {e}", path.display());
            Map::new()
        }
    }
}

fn write_object(path: &Path, object: &Map<String, Value>) -> io::Result<()> {
    let text = serde_json::to_string_pretty(object).expect("a JSON object always serializes");
    atomic_write(path, format!("{text}\n").as_bytes())
}

/// The artist remembered from the last save, in the given config folder.
pub fn remembered_artist_in(config_dir: &Path) -> String {
    clean_tag(read_object(&config_path_in(config_dir)).get("episode_artist"))
}

/// Makes `artist` the default for the next set. Best effort: a failure is logged.
pub fn remember_artist_in(config_dir: &Path, artist: &str) {
    let artist = clean_str(artist);
    if artist.is_empty() || artist == remembered_artist_in(config_dir) {
        return;
    }
    let path = config_path_in(config_dir);
    let mut config = read_object(&path);
    config.insert("episode_artist".into(), Value::String(artist));
    if let Err(e) = write_object(&path, &config) {
        log::warn!(
            "could not remember the episode artist in {}: {e}",
            path.display()
        );
    }
}

/// The set's tags: what was saved, else the defaults. Each key falls back on its own, so a
/// hand-edited file missing one still gets the other's default; a saved `""` stays blank.
pub fn episode_tags_in(config_dir: &Path, tracklist_path: &Path) -> EpisodeTags {
    let saved = read_object(&episode_path_for(tracklist_path));
    EpisodeTags {
        title: match saved.get("title") {
            Some(v) => clean_tag(Some(v)),
            None => default_title(tracklist_path),
        },
        artist: match saved.get("artist") {
            Some(v) => clean_tag(Some(v)),
            None => remembered_artist_in(config_dir),
        },
    }
}

/// Stores the set's title and artist, and remembers the artist for the next set. The file is
/// only rewritten when it would change.
pub fn save_episode_tags_in(
    config_dir: &Path,
    tracklist_path: &Path,
    title: &str,
    artist: &str,
) -> io::Result<()> {
    let artist = clean_str(artist);
    let mut tags = Map::new();
    tags.insert("title".into(), Value::String(clean_str(title)));
    tags.insert("artist".into(), Value::String(artist.clone()));
    let path = episode_path_for(tracklist_path);
    if read_object(&path) != tags {
        write_object(&path, &tags)?;
    }
    remember_artist_in(config_dir, &artist);
    Ok(())
}

/// [`episode_tags_in`] with the real config folder.
pub fn episode_tags(tracklist_path: &Path) -> EpisodeTags {
    episode_tags_in(&config_dir(), tracklist_path)
}

/// [`save_episode_tags_in`] with the real config folder.
pub fn save_episode_tags(tracklist_path: &Path, title: &str, artist: &str) -> io::Result<()> {
    save_episode_tags_in(&config_dir(), tracklist_path, title, artist)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const SET: &str = "2026-09-23-Keys-Lounge_tracklist.md";

    struct Env {
        dir: tempfile::TempDir,
    }

    impl Env {
        fn new() -> Self {
            Env {
                dir: tempfile::tempdir().unwrap(),
            }
        }
        fn config(&self) -> PathBuf {
            self.dir.path().join("config/hindsight")
        }
        fn set(&self, name: &str) -> PathBuf {
            self.dir.path().join(name)
        }
        fn tags(&self, name: &str) -> EpisodeTags {
            episode_tags_in(&self.config(), &self.set(name))
        }
        fn save(&self, name: &str, title: &str, artist: &str) {
            save_episode_tags_in(&self.config(), &self.set(name), title, artist).unwrap();
        }
    }

    fn tags(title: &str, artist: &str) -> EpisodeTags {
        EpisodeTags {
            title: title.into(),
            artist: artist.into(),
        }
    }

    #[test]
    fn the_file_sits_beside_the_tracklist() {
        assert_eq!(
            episode_path_for(Path::new("/s").join(SET).as_path()),
            Path::new("/s/2026-09-23-Keys-Lounge_episode.json")
        );
    }

    #[test]
    fn the_default_title_is_the_date_the_set_is_named_for() {
        for (name, title) in [
            (SET, "September 23, 2026"),
            ("2026-01-05_tracklist.md", "January 5, 2026"),
            ("Keys-Lounge_tracklist.md", ""),
            ("2026-13-40-Nope_tracklist.md", ""),
            ("20260923-set_tracklist.md", ""),
            ("2026-09-233_tracklist.md", ""),
            ("2026-09-2_tracklist.md", ""),
            ("2026-02-29_tracklist.md", ""),
            ("2028-02-29x_tracklist.md", "February 29, 2028"),
            ("0999-01-02_tracklist.md", "January 2, 999"),
            ("0000-01-01_tracklist.md", ""),
            ("Ä026-09-23_tracklist.md", ""),
            // Python's \d is any decimal digit, so an Arabic-Indic one blocks the date too.
            ("2026-09-23\u{663}_tracklist.md", ""),
            ("2026-09-23\u{b2}_tracklist.md", "September 23, 2026"),
        ] {
            assert_eq!(default_title(Path::new(name)), title, "{name}");
        }
    }

    #[test]
    fn clean_tag_is_one_trimmed_capped_line() {
        assert_eq!(clean_tag(Some(&json!("  DJ \n Disarray\t"))), "DJ Disarray");
        assert_eq!(clean_tag(None), "");
        assert_eq!(clean_tag(Some(&json!(42))), "");
        assert_eq!(clean_tag(Some(&json!("x".repeat(500)))).len(), 200);
        // Characters, not bytes, as Python slices.
        assert_eq!(
            clean_tag(Some(&json!("ı".repeat(500)))).chars().count(),
            200
        );
    }

    #[test]
    fn a_new_set_gets_the_defaults() {
        assert_eq!(Env::new().tags(SET), tags("September 23, 2026", ""));
    }

    #[test]
    fn saving_stores_the_set_and_remembers_the_artist() {
        let env = Env::new();
        env.save(SET, " Keys Lounge ", "DJ Disarray");
        assert_eq!(env.tags(SET), tags("Keys Lounge", "DJ Disarray"));
        assert_eq!(remembered_artist_in(&env.config()), "DJ Disarray");
        assert_eq!(
            env.tags("2026-10-01-Next_tracklist.md"),
            tags("October 1, 2026", "DJ Disarray")
        );
        assert_eq!(
            fs::read_to_string(episode_path_for(&env.set(SET))).unwrap(),
            "{\n  \"title\": \"Keys Lounge\",\n  \"artist\": \"DJ Disarray\"\n}\n"
        );
    }

    #[test]
    fn a_saved_blank_stays_blank_and_is_not_remembered() {
        let env = Env::new();
        env.save("2026-01-01-a_tracklist.md", "", "DJ Disarray");
        env.save(SET, "", "");
        assert_eq!(env.tags(SET), tags("", ""));
        assert_eq!(remembered_artist_in(&env.config()), "DJ Disarray");
    }

    #[test]
    fn remembering_keeps_the_rest_of_the_config() {
        let env = Env::new();
        fs::create_dir_all(env.config()).unwrap();
        fs::write(config_path_in(&env.config()), r#"{"other": 1}"#).unwrap();
        env.save(SET, "T", "DJ Disarray");
        let config: Value =
            serde_json::from_str(&fs::read_to_string(config_path_in(&env.config())).unwrap())
                .unwrap();
        assert_eq!(config, json!({"other": 1, "episode_artist": "DJ Disarray"}));
    }

    #[test]
    fn a_hand_edited_file_degrades_to_defaults() {
        for junk in ["not json", "[1, 2]", r#"{"title": 5}"#] {
            let env = Env::new();
            fs::write(episode_path_for(&env.set(SET)), junk).unwrap();
            let got = env.tags(SET);
            assert_eq!(got.artist, "", "{junk}");
            assert!(["", "September 23, 2026"].contains(&got.title.as_str()));
        }
    }

    #[test]
    fn an_unchanged_save_leaves_the_file_alone() {
        let env = Env::new();
        env.save(SET, "T", "A");
        let path = episode_path_for(&env.set(SET));
        // Same values, other order and spacing: equal, so not rewritten.
        fs::write(&path, r#"{"artist": "A", "title": "T"}"#).unwrap();
        env.save(SET, "T", "A");
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            r#"{"artist": "A", "title": "T"}"#
        );
    }
}
