//! `<set>_progress.json`: every sample a run took. Resume is replay, so this list *is* the
//! run's state; the engine is rebuilt by folding it.
//!
//! Version 2 is `{"version": 2, "audio_duration": D, "probes": [...]}`. Hindsight adds two keys
//! Python's reader ignores (it reads keys by name): `ended`, how the run stopped, and
//! `tracklist_sha256`, the hash of the tracklist it wrote, so the editor can tell whether it
//! was edited since. The legacy file, from the sequential scan, is a bare `[[seconds, info],
//! ...]` list: it already carried times, so it reads as 30-second coverage samples and resumes
//! as an adaptive run. The first save writes version 2.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::fileio::atomic_write;

pub const PROGRESS_VERSION: u64 = 2;
/// A legacy sample's window: the sequential scan's fixed 30 seconds.
pub const LEGACY_WINDOW: f64 = 30.0;

/// One sample: where it looked and what came back. The engine's `Probe`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Probe {
    /// Where the window starts, in seconds.
    pub t: f64,
    /// How long the window is, in seconds.
    pub window: f64,
    /// `coverage`, `refine` or `manual` (asked for in the editor).
    pub purpose: String,
    /// Shazam's answer as stored, or `None` when nothing matched. Kept as JSON so a re-save
    /// writes back every key it read; M2's engine reads the fields it needs from it.
    pub result: Option<Value>,
    #[serde(default)]
    pub offsets: Option<Vec<Value>>,
}

impl Probe {
    pub fn mid(&self) -> f64 {
        self.t + self.window / 2.0
    }
}

/// How a run stopped.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Ended {
    Converged,
    FinishedEarly,
    Budget,
    Throttled,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Progress {
    /// Seconds. `None` for a legacy file, which never recorded it.
    pub audio_duration: Option<f64>,
    pub probes: Vec<Probe>,
    pub ended: Option<Ended>,
    pub tracklist_sha256: Option<String>,
}

#[derive(Serialize)]
struct V2<'a> {
    version: u64,
    audio_duration: Option<f64>,
    probes: &'a [Probe],
    #[serde(skip_serializing_if = "Option::is_none")]
    ended: Option<Ended>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tracklist_sha256: Option<&'a str>,
}

/// `<set>_progress.json` beside the tracklist: the tracklist's own `<stem>_tracklist.md` name
/// is the surest key, and the audio's stem covers a tracklist that was renamed.
pub fn progress_path_for(tracklist_path: &Path, audio_path: Option<&Path>) -> PathBuf {
    let name = tracklist_path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy();
    let stem = match (name.strip_suffix("_tracklist.md"), audio_path) {
        (Some(stem), _) => stem.to_string(),
        (None, Some(audio)) => audio
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .into(),
        (None, None) => tracklist_path
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .into(),
    };
    tracklist_path.with_file_name(format!("{stem}_progress.json"))
}

fn invalid(why: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, why.into())
}

/// Reads a progress file, either version. A missing file is an empty run.
pub fn load_progress(path: &Path) -> io::Result<Progress> {
    let raw = match fs::read(path) {
        Ok(raw) => raw,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Progress::default()),
        Err(e) => return Err(e),
    };
    let data: Value = serde_json::from_slice(&raw).map_err(|e| invalid(e.to_string()))?;
    parse(data)
}

fn parse(data: Value) -> io::Result<Progress> {
    match data {
        Value::Object(mut o) if o.get("version").and_then(Value::as_f64) == Some(2.0) => {
            let probes = o.remove("probes").unwrap_or(Value::Null);
            Ok(Progress {
                audio_duration: o.get("audio_duration").and_then(Value::as_f64),
                probes: serde_json::from_value(probes).map_err(|e| invalid(e.to_string()))?,
                ended: o
                    .remove("ended")
                    .and_then(|v| serde_json::from_value(v).ok()),
                tracklist_sha256: o
                    .get("tracklist_sha256")
                    .and_then(Value::as_str)
                    .map(String::from),
            })
        }
        // Python's `data or []`: an empty object reads as an empty legacy list.
        Value::Object(o) if o.is_empty() => Ok(Progress::default()),
        Value::Array(rows) => {
            let probes = rows
                .into_iter()
                .map(|row| match row {
                    Value::Array(pair) if pair.len() == 2 => {
                        let mut pair = pair.into_iter();
                        let t = pair.next().and_then(|v| v.as_f64());
                        let info = pair.next().unwrap_or(Value::Null);
                        Ok(Probe {
                            t: t.ok_or_else(|| invalid("a legacy sample's time isn't a number"))?,
                            window: LEGACY_WINDOW,
                            purpose: "coverage".into(),
                            result: (!info.is_null()).then_some(info),
                            offsets: None,
                        })
                    }
                    _ => Err(invalid("a legacy sample isn't a [time, info] pair")),
                })
                .collect::<io::Result<_>>()?;
            Ok(Progress {
                probes,
                ..Progress::default()
            })
        }
        Value::Null => Ok(Progress::default()),
        _ => Err(invalid("not a progress file")),
    }
}

/// Writes the progress file as version 2, atomically: the editor re-reads it while a run is
/// still writing it, and must never see half a probe list.
pub fn save_progress(path: &Path, progress: &Progress) -> io::Result<()> {
    let file = V2 {
        version: PROGRESS_VERSION,
        audio_duration: progress.audio_duration,
        probes: &progress.probes,
        ended: progress.ended,
        tracklist_sha256: progress.tracklist_sha256.as_deref(),
    };
    let text = serde_json::to_string_pretty(&file).map_err(|e| invalid(e.to_string()))?;
    atomic_write(path, text.as_bytes())
}

/// Adds one sample to a saved run, creating the file if the set has none: the editor's
/// click-to-sample. A legacy file converts to version 2 on the way through, as a resume would.
pub fn append_probe(path: &Path, probe: Probe, duration: Option<f64>) -> io::Result<()> {
    let mut progress = load_progress(path)?;
    progress.audio_duration = progress
        .audio_duration
        .filter(|d| *d != 0.0)
        .or(duration.filter(|d| *d != 0.0))
        .or(Some(0.0));
    progress.probes.push(probe);
    save_progress(path, &progress)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn info() -> Value {
        json!({"artist": "X", "title": "A", "confidence": 0.9})
    }

    fn probe(t: f64, window: f64, purpose: &str, result: Option<Value>) -> Probe {
        Probe {
            t,
            window,
            purpose: purpose.into(),
            result,
            offsets: None,
        }
    }

    #[test]
    fn v2_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        let progress = Progress {
            audio_duration: Some(3600.0),
            probes: vec![
                Probe {
                    offsets: Some(vec![json!({"offset": 10.0, "timeskew": 0.0})]),
                    ..probe(100.0, 30.0, "coverage", Some(info()))
                },
                probe(200.0, 12.0, "refine", None),
            ],
            ..Progress::default()
        };
        save_progress(&path, &progress).unwrap();
        assert_eq!(load_progress(&path).unwrap(), progress);
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.starts_with("{\n  \"version\": 2,\n  \"audio_duration\": 3600.0,"));
        assert!(!text.contains("ended") && !text.contains("tracklist_sha256"));
    }

    #[test]
    fn the_new_keys_round_trip_and_python_shaped_files_lack_them() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        let progress = Progress {
            audio_duration: Some(60.0),
            ended: Some(Ended::FinishedEarly),
            tracklist_sha256: Some("ab".repeat(32)),
            ..Progress::default()
        };
        save_progress(&path, &progress).unwrap();
        assert!(
            fs::read_to_string(&path)
                .unwrap()
                .contains("\"finished_early\"")
        );
        assert_eq!(load_progress(&path).unwrap(), progress);
    }

    #[test]
    fn the_legacy_list_converts_to_coverage_probes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        fs::write(
            &path,
            json!([[0, info()], [30, null], [60, info()]]).to_string(),
        )
        .unwrap();
        let loaded = load_progress(&path).unwrap();
        assert_eq!(loaded.audio_duration, None);
        let ts: Vec<f64> = loaded.probes.iter().map(|p| p.t).collect();
        assert_eq!(ts, [0.0, 30.0, 60.0]);
        assert!(
            loaded
                .probes
                .iter()
                .all(|p| p.window == 30.0 && p.purpose == "coverage")
        );
        assert_eq!(loaded.probes[1].result, None);
    }

    #[test]
    fn a_missing_file_is_an_empty_run() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(
            load_progress(&dir.path().join("absent.json")).unwrap(),
            Progress::default()
        );
    }

    #[test]
    fn other_shapes_are_errors_not_empty_runs() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        for junk in [
            "{not json",
            r#"{"version": 3, "probes": []}"#,
            r#"[[0]]"#,
            r#"[["x", null]]"#,
            "42",
        ] {
            fs::write(&path, junk).unwrap();
            assert!(load_progress(&path).is_err(), "{junk}");
        }
        for empty in ["{}", "[]", "null"] {
            fs::write(&path, empty).unwrap();
            assert_eq!(
                load_progress(&path).unwrap(),
                Progress::default(),
                "{empty}"
            );
        }
    }

    #[test]
    fn floats_come_back_exactly() {
        // The default serde_json parser was an ulp off on this one, from a real file.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        let text = r#"{"version": 2, "audio_duration": 14430.504, "probes": [{"t": 0.30000000000000004,
            "window": 30.0, "purpose": "coverage", "result": null,
            "offsets": [{"offset": 108.76374218699999, "timeskew": 0.0012657642}]}]}"#;
        fs::write(&path, text).unwrap();
        save_progress(&path, &load_progress(&path).unwrap()).unwrap();
        let saved = fs::read_to_string(&path).unwrap();
        assert!(saved.contains("108.76374218699999"), "{saved}");
        assert!(saved.contains("0.30000000000000004"));
    }

    #[test]
    fn version_two_point_oh_is_version_two() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        fs::write(
            &path,
            r#"{"version": 2.0, "audio_duration": null, "probes": []}"#,
        )
        .unwrap();
        assert_eq!(load_progress(&path).unwrap(), Progress::default());
    }

    #[test]
    fn appending_creates_converts_and_keeps_the_saved_duration() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        append_probe(&path, probe(5.0, 12.0, "manual", None), Some(100.0)).unwrap();
        let p = load_progress(&path).unwrap();
        assert_eq!((p.audio_duration, p.probes.len()), (Some(100.0), 1));
        append_probe(&path, probe(9.0, 12.0, "manual", None), Some(999.0)).unwrap();
        assert_eq!(load_progress(&path).unwrap().audio_duration, Some(100.0));

        fs::write(&path, json!([[0, info()]]).to_string()).unwrap();
        append_probe(&path, probe(40.0, 12.0, "manual", Some(info())), None).unwrap();
        let p = load_progress(&path).unwrap();
        assert_eq!(p.audio_duration, Some(0.0));
        assert_eq!(p.probes.len(), 2);
        assert!(
            fs::read_to_string(&path)
                .unwrap()
                .contains("\"version\": 2")
        );
    }

    #[test]
    fn the_progress_file_sits_beside_the_tracklist() {
        let dir = Path::new("/s");
        for (md, audio, expected) in [
            (
                "2026-09-23-Keys-Lounge_tracklist.md",
                None,
                "2026-09-23-Keys-Lounge_progress.json",
            ),
            ("renamed.md", Some("set.mp3"), "set_progress.json"),
            ("renamed.md", None, "renamed_progress.json"),
        ] {
            let audio = audio.map(|a| dir.join(a));
            assert_eq!(
                progress_path_for(&dir.join(md), audio.as_deref()),
                dir.join(expected)
            );
        }
    }
}
