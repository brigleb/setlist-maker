//! `<set>_events.jsonl`: one line per thing a run did, appended, so a resumed run extends the
//! history. Each line is Python's shape, `{"at": <unix seconds, 2 places>, ...the event}`.

use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};

use crate::engine::py;

pub struct EventLog {
    file: File,
}

impl EventLog {
    pub fn open(path: &Path) -> io::Result<EventLog> {
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(EventLog { file })
    }

    /// Appends `event` (an object) with `at` first. Written whole and unbuffered, so a
    /// `tail -f` sees each one land.
    pub fn write(&mut self, event: Value) -> io::Result<()> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let mut line = Map::new();
        line.insert("at".into(), py::round_to(now, 2).into());
        if let Value::Object(fields) = event {
            line.extend(fields);
        }
        let mut text = Value::Object(line).to_string();
        text.push('\n');
        self.file.write_all(text.as_bytes())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn appends_with_at_first() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.jsonl");
        EventLog::open(&path)
            .unwrap()
            .write(json!({"type": "a"}))
            .unwrap();
        EventLog::open(&path)
            .unwrap()
            .write(json!({"type": "b", "at": 1}))
            .unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<Value> = text
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0]["type"], "a");
        assert!(text.lines().all(|l| l.starts_with(r#"{"at":"#)));
    }
}
