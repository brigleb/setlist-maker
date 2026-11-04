//! `hindsight replay`: folds a saved run one probe at a time and prints, after each probe
//! (and once before the first), everything the engine would tell a run: the events, the
//! segments and what was dropped, the next probe, and the progress numbers. Resume is
//! replay, so if this matches Python's at every prefix, a resumed run continues the same.
//! `scripts/parity_m2.py` runs Python's side and compares the bytes.

use std::io::{self, BufWriter, Write};
use std::path::Path;

use hindsight_core::engine::boundary::{BoundaryEngine, EngineConfig, Event};
use hindsight_core::model::load_progress;
use serde_json::{Value, json};

use crate::pyjson;

pub fn run(path: &Path, duration: Option<f64>) -> io::Result<()> {
    let progress = load_progress(path)?;
    // Python's `audio_duration or duration`: a 0 counts as missing.
    let duration = progress
        .audio_duration
        .filter(|d| *d != 0.0)
        .or(duration)
        .ok_or_else(|| io::Error::other("the file has no audio_duration; pass --duration"))?;
    let mut engine = BoundaryEngine::new(duration, EngineConfig::default());
    let mut out = BufWriter::new(io::stdout().lock());
    writeln!(out, "{}", line(&engine, 0, &[]))?;
    for (n, probe) in progress.probes.into_iter().enumerate() {
        let events = engine.add_probe(probe);
        writeln!(out, "{}", line(&engine, n + 1, &events))?;
    }
    out.flush()
}

fn line(engine: &BoundaryEngine, n: usize, events: &[Event]) -> String {
    let (segments, drops) = engine.segments();
    let next = engine
        .next_probe()
        .map(|p| json!({"t": p.t, "window": p.window, "purpose": p.purpose.as_str()}));
    let value = json!({
        "n": n,
        "events": events,
        "segments": segments,
        "drops": drops,
        "next": next.unwrap_or(Value::Null),
        "stats": engine.boundary_stats(),
        "remaining": engine.estimated_probes_remaining(),
        "max_ratio": engine.max_ratio(),
        "max_boundary_width": engine.max_boundary_width(),
    });
    pyjson::to_string(&value)
}
