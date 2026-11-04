//! The adaptive driver: everything impure around the pure engine. It asks Shazam about the
//! window the engine wants next, paced 15s apart, saves the progress file after every answer,
//! appends `<set>_events.jsonl`, and stops when the engine converges, on the budget, when
//! asked to, or when Shazam stops answering.
//!
//! Resume is replay: the probe list is the run's whole state, and the engine is rebuilt by
//! folding it. A sample Shazam didn't answer is never saved, so a resumed run asks again; a
//! throttled answer never looks like "unidentified".

mod events;
mod pacing;

use std::io;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::audio::{AudioError, Recording, format_timestamp};
use crate::engine::boundary::{BoundaryEngine, Dropped, EngineConfig, Segment};
use crate::model::progress::{Ended, Probe, Progress, load_progress, save_progress};
use crate::model::{CorrectionsDb, Track, Tracklist, save_tracklist};
use crate::shazam::{self, Client, Heard, NoAnswer};

pub use events::EventLog;
pub use pacing::{Clock, Pacing, StopSignal};

/// Seconds between Shazam calls. Field evidence puts the safe rate at 4–6 a minute.
pub const DELAY: Duration = Duration::from_secs(15);
/// The waits before each retry of a sample Shazam didn't answer: about 5 minutes in all.
pub const BACKOFF: [Duration; 4] = [
    Duration::from_secs(20),
    Duration::from_secs(40),
    Duration::from_secs(80),
    Duration::from_secs(160),
];

/// Where a set's files go: beside the audio, or in `output_dir`.
#[derive(Clone, Debug, PartialEq)]
pub struct SetPaths {
    pub tracklist: PathBuf,
    pub progress: PathBuf,
    pub events: PathBuf,
}

impl SetPaths {
    pub fn new(audio: &Path, output_dir: Option<&Path>) -> SetPaths {
        let dir = output_dir.unwrap_or_else(|| audio.parent().unwrap_or(Path::new("")));
        let stem = audio.file_stem().unwrap_or_default().to_string_lossy();
        SetPaths {
            tracklist: dir.join(format!("{stem}_tracklist.md")),
            progress: dir.join(format!("{stem}_progress.json")),
            events: dir.join(format!("{stem}_events.jsonl")),
        }
    }
}

/// What a run asks: what Shazam hears in `[t, t + window)`.
pub trait Listener {
    fn listen(&mut self, t: f64, window: f64) -> Result<Heard, NoAnswer>;
}

/// The real listener: decode the window, fingerprint its centred 10 seconds, ask Shazam.
pub struct ShazamListener {
    recording: Recording,
    client: Client,
}

impl ShazamListener {
    pub fn new(recording: Recording) -> Result<ShazamListener, NoAnswer> {
        Ok(ShazamListener {
            recording,
            client: Client::new()?,
        })
    }
}

impl Listener for ShazamListener {
    fn listen(&mut self, t: f64, window: f64) -> Result<Heard, NoAnswer> {
        let pcm = self
            .recording
            .window_16k(t, window)
            .map_err(|e: AudioError| NoAnswer::Failed(e.to_string()))?;
        self.client.recognize(&shazam::fingerprint(&pcm))
    }
}

/// What a run tells its front end as it goes.
pub enum Note<'a> {
    Resumed(usize),
    Probe(&'a Probe),
    /// No answer; waiting `wait` before retry `attempt` of `of`.
    Retrying {
        why: &'a NoAnswer,
        wait: Duration,
        attempt: usize,
        of: usize,
    },
}

pub struct Options {
    pub delay: Duration,
    pub backoff: Vec<Duration>,
    pub budget: Option<Duration>,
    pub engine: EngineConfig,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            delay: DELAY,
            backoff: BACKOFF.to_vec(),
            budget: None,
            engine: EngineConfig::default(),
        }
    }
}

/// How a run ended, and what it found.
#[derive(Debug)]
pub struct Outcome {
    pub ended: Ended,
    pub duration: f64,
    pub probes: Vec<Probe>,
    pub segments: Vec<Segment>,
    /// Why Shazam stopped answering, when that's what ended the run.
    pub gave_up: Option<NoAnswer>,
    /// Probes the engine still expected to need when the run stopped.
    pub remaining: u64,
}

/// Identifies a recording of `duration` seconds, resuming from `resume`'s probes.
pub fn identify(
    duration: f64,
    resume: Vec<Probe>,
    listener: &mut dyn Listener,
    pacing: &mut dyn Pacing,
    options: &Options,
    paths: &SetPaths,
    note: &mut dyn FnMut(Note),
) -> io::Result<Outcome> {
    let mut engine = BoundaryEngine::new(duration, options.engine.clone());
    let mut progress = Progress {
        audio_duration: Some(duration),
        probes: Vec::new(),
        ended: None,
        tracklist_sha256: None,
    };
    for probe in resume {
        engine.add_probe(probe.clone()); // replay; the run that took it logged its events
        progress.probes.push(probe);
    }
    if !progress.probes.is_empty() {
        note(Note::Resumed(progress.probes.len()));
    }
    let mut events = EventLog::open(&paths.events)?;
    let mut gave_up = None;

    let (ended, reason) = loop {
        if pacing.stopped() {
            break (Ended::FinishedEarly, "interrupted");
        }
        if options.budget.is_some_and(|b| pacing.elapsed() >= b) {
            events.write(
                json!({"type": "budget_exhausted", "after_probes": progress.probes.len()}),
            )?;
            break (Ended::Budget, "budget");
        }
        let Some(plan) = engine.next_probe() else {
            break (Ended::Converged, "converged");
        };
        let heard = match ask(listener, pacing, options, plan.t, plan.window, note) {
            Ok(heard) => heard,
            Err(why) => {
                if pacing.stopped() {
                    break (Ended::FinishedEarly, "interrupted");
                }
                let throttled = why == NoAnswer::Throttled;
                gave_up = Some(why);
                break if throttled {
                    (Ended::Throttled, "throttled")
                } else {
                    (Ended::FinishedEarly, "no_answer")
                };
            }
        };
        let probe = Probe {
            t: plan.t,
            window: plan.window,
            purpose: plan.purpose.as_str().into(),
            result: heard.result,
            offsets: heard.offsets,
        };
        for event in engine.add_probe(probe.clone()) {
            events.write(serde_json::to_value(event).expect("events serialize"))?;
        }
        progress.probes.push(probe);
        save_progress(&paths.progress, &progress)?;
        note(Note::Probe(progress.probes.last().expect("just pushed")));
        if engine.next_probe().is_some() && !pacing.stopped() {
            pacing.wait(options.delay);
        }
    };

    let (segments, drops) = engine.segments();
    for drop in &drops {
        events.write(drop_value(drop))?;
    }
    events
        .write(json!({"type": "finalized", "probes": progress.probes.len(), "reason": reason}))?;
    Ok(Outcome {
        ended,
        duration,
        remaining: engine.estimated_probes_remaining(),
        probes: progress.probes,
        segments,
        gave_up,
    })
}

fn drop_value(drop: &Dropped) -> Value {
    serde_json::to_value(drop).expect("drops serialize")
}

/// One sample, retried with backoff while Shazam doesn't answer.
fn ask(
    listener: &mut dyn Listener,
    pacing: &mut dyn Pacing,
    options: &Options,
    t: f64,
    window: f64,
    note: &mut dyn FnMut(Note),
) -> Result<Heard, NoAnswer> {
    let mut waits = options.backoff.iter().enumerate();
    loop {
        let why = match listener.listen(t, window) {
            Ok(heard) => return Ok(heard),
            Err(why) => why,
        };
        let Some((i, &wait)) = waits.next() else {
            return Err(why);
        };
        note(Note::Retrying {
            why: &why,
            wait,
            attempt: i + 1,
            of: options.backoff.len(),
        });
        if pacing.wait(wait) {
            return Err(why);
        }
    }
}

/// Resume: the saved probes, unless `fresh`. A legacy sequential list reads as coverage
/// probes; the first save rewrites it as version 2.
pub fn saved_probes(paths: &SetPaths, fresh: bool) -> io::Result<Vec<Probe>> {
    if fresh {
        return Ok(Vec::new());
    }
    Ok(load_progress(&paths.progress)?.probes)
}

/// The tracklist a run's segments make, with learned corrections applied (the original
/// names kept). One row per segment: the engine already merged each track's probes.
pub fn segments_to_tracklist(
    segments: &[Segment],
    source_file: &str,
    corrections: Option<&CorrectionsDb>,
    generated_on: String,
) -> Tracklist {
    let text = |info: &Value, key: &str| {
        info.get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    let tracks = segments
        .iter()
        .map(|seg| {
            let timestamp = seg.start.round_ties_even() as i64;
            let Some(info) = &seg.info else {
                return Track::new(timestamp, "", "");
            };
            let opt = |key: &str| info.get(key).and_then(Value::as_str).map(String::from);
            let (artist, title) = (text(info, "artist"), text(info, "title"));
            let mut track = Track {
                shazam_url: opt("shazam_url"),
                album: opt("album"),
                coverart_url: opt("coverart_url"),
                confidence: info.get("confidence").and_then(Value::as_f64),
                ..Track::new(timestamp, artist.clone(), title.clone())
            };
            if let Some((a, t)) = corrections.and_then(|db| db.get_correction(&artist, &title)) {
                (track.artist, track.title) = (a, t);
                track.original_artist = Some(artist);
                track.original_title = Some(title);
            }
            track
        })
        .collect();
    Tracklist {
        source_file: source_file.into(),
        tracks,
        generated_on: Some(generated_on),
        summary: None,
    }
}

/// Writes the tracklist and its sidecar, then the progress file's last save: how the run
/// ended and the hash of the tracklist it wrote, so the editor can tell if it was edited since.
pub fn finalize(tracklist: &Tracklist, outcome: &Outcome, paths: &SetPaths) -> io::Result<()> {
    save_tracklist(tracklist, &paths.tracklist, None)?;
    let written = std::fs::read(&paths.tracklist)?;
    let sha = Sha256::digest(&written);
    let progress = Progress {
        audio_duration: Some(outcome.duration),
        probes: outcome.probes.clone(),
        ended: Some(outcome.ended),
        tracklist_sha256: Some(sha.iter().map(|b| format!("{b:02x}")).collect()),
    };
    save_progress(&paths.progress, &progress)
}

/// Now, as a tracklist's "Generated on" stamp.
pub fn now_stamp() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M").to_string()
}

/// One plain line per probe: `  > 5:10  +   90%  B - Beta`.
pub fn probe_line(probe: &Probe) -> String {
    let tag = match probe.purpose.as_str() {
        "coverage" => '.',
        "refine" => '>',
        "manual" => '*',
        _ => ' ',
    };
    let time = format_timestamp(probe.t as i64);
    let Some(info) = &probe.result else {
        return format!("  {tag} {time:>7}  -  not identified");
    };
    let conf = match info.get("confidence").and_then(Value::as_f64) {
        Some(c) => format!("{:>3}%", (c * 100.0).round_ties_even() as i64),
        None => " -- ".into(),
    };
    let name = |k| info.get(k).and_then(Value::as_str).unwrap_or("");
    format!(
        "  {tag} {time:>7}  +  {conf}  {} - {}",
        name("artist"),
        name("title")
    )
}

#[cfg(test)]
mod tests;
