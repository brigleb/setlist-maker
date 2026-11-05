//! What one window has open: nothing (the drop screen), a run still identifying, or a set
//! being edited, and the moves between them. Python's `EditorContext`, `LiveRun` and
//! `WatchSession`, folded into one state behind one lock.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use serde_json::Value;

use hindsight_core::audio::undecoded_seconds;
use hindsight_core::engine::boundary::{BoundaryEngine, EngineConfig};
use hindsight_core::model::progress::{Ended, load_progress, progress_path_for};
use hindsight_core::model::store::AUDIO_EXTENSIONS;
use hindsight_core::model::{CorrectionsDb, Probe, Tracklist, find_audio_file, load_tracklist};
use hindsight_core::run::{
    self, Clock, Existing, Listener, Note, Options, Questions, Sampler, SetPaths, StopSignal,
};

use crate::Editor;

/// Click-to-sample's Shazam, opened on the first click (or why it couldn't be).
pub type SamplerSlot = Arc<OnceLock<Result<Sampler<Box<dyn Listener + Send>>, String>>>;

/// A set open for editing.
pub struct OpenSet {
    pub tracklist_path: PathBuf,
    pub audio: Option<PathBuf>,
    pub progress: PathBuf,
    pub tracklist: Tracklist,
    pub corrections: CorrectionsDb,
    /// A run that stopped early and can carry on, and whether the tracklist was edited since.
    pub unfinished: Option<(Ended, bool)>,
    /// The recording's length from its packets, once asked for.
    pub duration: OnceLock<Option<f64>>,
    /// Click-to-sample, opened on the first click.
    pub sampler: SamplerSlot,
    /// When the run that just finished last asked Shazam, so the first click waits its turn.
    pub last_call: Option<Instant>,
}

impl OpenSet {
    pub fn duration(&self) -> Option<f64> {
        *self.duration.get_or_init(|| {
            let audio = self.audio.as_ref().filter(|a| a.is_file())?;
            undecoded_seconds(audio).ok()
        })
    }
}

/// How a live run is getting on, as its thread reports it.
#[derive(Clone, Debug, Default)]
pub struct LiveStatus {
    /// Seconds, once the recording has been measured.
    pub duration: Option<f64>,
    /// Shazam is still throttling after the backoff: the run waits for Resume.
    pub paused: Option<String>,
}

/// What the page draws while a run is live: the progress file replayed through a fresh engine,
/// which is the run's own state, since resume is replay.
#[derive(Clone)]
pub struct Snapshot {
    pub tracklist: Tracklist,
    pub next: Option<(f64, f64, String)>,
    pub probes: Vec<Probe>,
    pub duration: Option<f64>,
}

/// The progress file's mtime (ns) and size, and the recording's length once known.
type SnapshotKey = (i128, u64, Option<u64>);

/// A run still identifying.
pub struct Live {
    pub id: u64,
    pub paths: SetPaths,
    pub audio: PathBuf,
    pub source_file: String,
    pub stop: Arc<StopSignal>,
    pub questions: Arc<Questions>,
    pub status: Arc<Mutex<LiveStatus>>,
    /// Stop rather than finish: leave without writing the tracklist (the progress stays).
    pub abort: Arc<AtomicBool>,
    pub thread: Option<JoinHandle<()>>,
    cache: Mutex<Option<(SnapshotKey, Snapshot)>>,
}

impl Live {
    /// The replay, cached on the progress file's mtime and size, so polling costs a `stat`
    /// until the run records another probe.
    pub fn snapshot(&self) -> Snapshot {
        let duration = self
            .status
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .duration;
        let key = std::fs::metadata(&self.paths.progress)
            .ok()
            .map(|m| {
                use std::os::unix::fs::MetadataExt;
                (
                    i128::from(m.mtime()) * 1_000_000_000 + i128::from(m.mtime_nsec()),
                    m.len(),
                )
            })
            .unwrap_or_default();
        // The length is part of the key: the replay before the recording is measured has none.
        let key = (key.0, key.1, duration.map(f64::to_bits));
        let mut cache = self.cache.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((k, snap)) = cache.as_ref()
            && *k == key
        {
            return snap.clone();
        }
        let snap = replay(&self.paths.progress, &self.source_file, duration)
            .or_else(|| cache.as_ref().map(|(_, s)| s.clone()))
            .unwrap_or_else(|| Snapshot {
                tracklist: Tracklist {
                    source_file: self.source_file.clone(),
                    ..Tracklist::default()
                },
                next: None,
                probes: Vec::new(),
                duration,
            });
        *cache = Some((key, snap.clone()));
        snap
    }
}

/// Python's `adaptive.live_snapshot`. `None` when the file won't read (the last good answer
/// is kept: the next probe rewrites it).
fn replay(progress: &Path, source_file: &str, duration_hint: Option<f64>) -> Option<Snapshot> {
    let saved = if progress.exists() {
        load_progress(progress).ok()?
    } else {
        Default::default()
    };
    let duration = saved.audio_duration.filter(|d| *d > 0.0).or(duration_hint);
    let empty = Tracklist {
        source_file: source_file.to_string(),
        ..Tracklist::default()
    };
    let Some(d) = duration.filter(|_| !saved.probes.is_empty()) else {
        return Some(Snapshot {
            tracklist: empty,
            next: None,
            probes: saved.probes,
            duration,
        });
    };
    let mut engine = BoundaryEngine::new(d, EngineConfig::default());
    for probe in &saved.probes {
        engine.add_probe(probe.clone());
    }
    let (segments, _) = engine.segments();
    let tracklist = run::segments_to_tracklist(&segments, source_file, None, String::new());
    let next = engine
        .next_probe()
        .map(|p| (p.t, p.window, p.purpose.as_str().to_string()));
    Some(Snapshot {
        tracklist: Tracklist {
            generated_on: None,
            ..tracklist
        },
        next,
        probes: saved.probes,
        duration,
    })
}

pub enum Phase {
    Empty,
    Live(Box<Live>),
    Editing(Box<OpenSet>),
}

/// How a live run ends when something else needs the window.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum EndRun {
    /// Write the tracklist from what was heard (Finish now).
    Finish,
    /// Leave without writing it; the progress file is kept, so opening the set again resumes.
    Stop,
}

impl EndRun {
    pub fn parse(v: Option<&Value>) -> Option<EndRun> {
        match v.and_then(Value::as_str) {
            Some("finish") => Some(EndRun::Finish),
            Some("stop") => Some(EndRun::Stop),
            _ => None,
        }
    }
}

/// What a path dropped on the window, or the Dock icon, would open.
#[derive(Debug, PartialEq)]
pub enum Opening {
    /// A tracklist (and the audio beside it, if any): the editor.
    Edit {
        tracklist: PathBuf,
        audio: Option<PathBuf>,
    },
    /// A recording with no finished tracklist: identify it, resuming any saved probes.
    Identify(PathBuf),
}

/// The inventory's "what a drop opens".
pub fn classify(path: &Path) -> Result<Opening, String> {
    let ext = path
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))
        .unwrap_or_default();
    if !path.is_file() {
        return Err(format!("{} isn't a file", path.display()));
    }
    if ext == ".md" {
        return Ok(Opening::Edit {
            tracklist: path.to_path_buf(),
            audio: find_audio_file(path),
        });
    }
    if !AUDIO_EXTENSIONS.contains(&ext.as_str()) {
        return Err(format!(
            "Hindsight opens a set's recording or its _tracklist.md, not {}",
            path.file_name().unwrap_or_default().to_string_lossy()
        ));
    }
    let paths = SetPaths::new(path, None);
    Ok(match run::existing(&paths) {
        Existing::Nothing => Opening::Identify(path.to_path_buf()),
        Existing::Finished(_) | Existing::Unfinished { .. } => Opening::Edit {
            tracklist: paths.tracklist,
            audio: Some(path.to_path_buf()),
        },
    })
}

impl Editor {
    pub(crate) fn corrections(&self) -> CorrectionsDb {
        CorrectionsDb::open(self.config.config_dir.join("corrections.json"))
    }

    /// The editor on a set, with learned corrections applied as Python's `run_web_editor` did.
    pub(crate) fn open_set(
        &self,
        tracklist_path: &Path,
        audio: Option<PathBuf>,
        last_call: Option<Instant>,
    ) -> Result<OpenSet, String> {
        let parsed = load_tracklist(tracklist_path)
            .map_err(|e| format!("can't read {}: {e}", tracklist_path.display()))?;
        for warning in &parsed.warnings {
            log::warn!("{}: {warning}", tracklist_path.display());
        }
        let mut tracklist = parsed.tracklist;
        let corrections = self.corrections();
        corrections.apply_corrections(&mut tracklist);
        let audio = audio.filter(|a| a.exists());
        let progress = progress_path_for(tracklist_path, audio.as_deref());
        let unfinished = match audio
            .as_deref()
            .map(|a| run::existing(&SetPaths::new(a, None)))
        {
            Some(Existing::Unfinished { ended, edited }) => Some((ended, edited)),
            _ => None,
        };
        Ok(OpenSet {
            tracklist_path: tracklist_path.to_path_buf(),
            audio,
            progress,
            tracklist,
            corrections,
            unfinished,
            duration: OnceLock::new(),
            sampler: Arc::new(OnceLock::new()),
            last_call,
        })
    }

    /// Opens `path` in this window, ending whatever was open first: a live run as `end` says
    /// (the page asked the user), a set being edited without saving (the page asked that too).
    pub fn open(self: &Arc<Self>, path: &Path, end: EndRun) -> Result<(), String> {
        let opening = classify(path)?;
        self.end_live(end);
        match opening {
            Opening::Edit { tracklist, audio } => {
                let set = self.open_set(&tracklist, audio, None)?;
                let mut state = self.state();
                state.phase = Phase::Editing(Box::new(set));
                state.revision += 1;
                state.notice = None;
            }
            Opening::Identify(audio) => self.start_live(&audio),
        }
        Ok(())
    }

    /// Back to the drop screen.
    pub fn close_set(&self, end: EndRun) {
        self.end_live(end);
        let mut state = self.state();
        state.phase = Phase::Empty;
        state.revision += 1;
    }

    /// Starts identifying `audio` in the background, resuming its saved probes.
    pub(crate) fn start_live(self: &Arc<Self>, audio: &Path) {
        let paths = SetPaths::new(audio, None);
        let mut state = self.state();
        state.live_runs += 1;
        let live = Live {
            id: state.live_runs,
            paths: paths.clone(),
            audio: audio.to_path_buf(),
            source_file: audio
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned(),
            stop: StopSignal::new(),
            questions: Questions::new(),
            status: Arc::new(Mutex::new(LiveStatus::default())),
            abort: Arc::new(AtomicBool::new(false)),
            thread: None,
            cache: Mutex::new(None),
        };
        let job = Job {
            id: live.id,
            audio: live.audio.clone(),
            paths,
            source_file: live.source_file.clone(),
            stop: live.stop.clone(),
            questions: live.questions.clone(),
            status: live.status.clone(),
            abort: live.abort.clone(),
        };
        let editor = self.clone();
        let thread = std::thread::Builder::new()
            .name("hindsight-run".into())
            .spawn(move || editor.run_live(job))
            .expect("spawn the run thread");
        state.phase = Phase::Live(Box::new(Live {
            thread: Some(thread),
            ..live
        }));
        state.revision += 1;
        state.notice = None;
    }

    /// Ends a live run, if there is one, and waits for its thread: Finish writes the
    /// tracklist and leaves the set open for editing; Stop leaves it unwritten. Waiting can
    /// take as long as the Shazam call in flight.
    pub fn end_live(&self, end: EndRun) {
        let thread = {
            let mut state = self.state();
            let Phase::Live(live) = &mut state.phase else {
                return;
            };
            if end == EndRun::Stop {
                live.abort.store(true, Ordering::SeqCst);
            }
            live.stop.request();
            live.thread.take()
        };
        if let Some(thread) = thread {
            let _ = thread.join();
        }
    }

    fn run_live(self: Arc<Self>, job: Job) {
        let result = self.identify(&job);
        let mut state = self.state();
        // A run that was replaced (another open) leaves the window to whatever replaced it.
        if !matches!(&state.phase, Phase::Live(l) if l.id == job.id) {
            return;
        }
        state.revision += 1; // every index the page held named a live guess
        match result {
            Ok(Some(set)) => state.phase = Phase::Editing(Box::new(set)),
            Ok(None) => state.phase = Phase::Empty,
            Err(why) => {
                log::warn!("{}: {why}", job.audio.display());
                state.notice = Some(why);
                state.phase = Phase::Empty;
            }
        }
    }

    /// The run itself: `Ok(None)` when stopped without finishing.
    fn identify(&self, job: &Job) -> Result<Option<OpenSet>, String> {
        let name = job.audio.display();
        let (mut listener, duration) = (self.config.listener)(&job.audio, ListenFor::Run)?;
        job.status
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .duration = Some(duration);
        let resume = run::saved_probes(&job.paths, false)
            .map_err(|e| format!("can't read {}: {e}", job.paths.progress.display()))?;
        let options = Options {
            delay: self.config.delay,
            backoff: self.config.backoff.clone(),
            questions: Some(job.questions.clone()),
            pause_when_throttled: true,
            ..Options::default()
        };
        let status = job.status.clone();
        let outcome = run::identify(
            duration,
            resume,
            &mut listener,
            &mut Clock::new(job.stop.clone()),
            &options,
            &job.paths,
            &mut |note| {
                let mut s = status.lock().unwrap_or_else(|e| e.into_inner());
                match note {
                    Note::Paused(why) => s.paused = Some(why.to_string()),
                    Note::Unpaused => s.paused = None,
                    _ => {}
                }
            },
        )
        .map_err(|e| format!("identifying {name} failed: {e}"))?;
        if job.abort.load(Ordering::SeqCst) {
            return Ok(None);
        }
        let corrections = self.corrections();
        let tracklist = run::segments_to_tracklist(
            &outcome.segments,
            &job.source_file,
            Some(&corrections),
            run::now_stamp(),
        );
        run::finalize(&tracklist, &outcome, &job.paths)
            .map_err(|e| format!("can't save {}: {e}", job.paths.tracklist.display()))?;
        self.open_set(
            &job.paths.tracklist,
            Some(job.audio.clone()),
            Some(Instant::now()),
        )
        .map(Some)
    }
}

struct Job {
    id: u64,
    audio: PathBuf,
    paths: SetPaths,
    source_file: String,
    stop: Arc<StopSignal>,
    questions: Arc<Questions>,
    status: Arc<Mutex<LiveStatus>>,
    abort: Arc<AtomicBool>,
}

/// What a listener is opened for: a run measures the whole recording (and refuses one still
/// syncing); click-to-sample needs it at once.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ListenFor {
    Run,
    Sample,
}

/// Opens a listener on a recording: the listener and the recording's length in seconds.
pub type OpenListener =
    dyn Fn(&Path, ListenFor) -> Result<(Box<dyn Listener + Send>, f64), String> + Send + Sync;

/// Shazam, on the real recording.
pub fn shazam_listener(
    audio: &Path,
    why: ListenFor,
) -> Result<(Box<dyn Listener + Send>, f64), String> {
    use hindsight_core::audio::Recording;
    let recording = match why {
        ListenFor::Run => Recording::open(audio, false),
        ListenFor::Sample => Recording::open_undecoded(audio),
    }
    .map_err(|e| format!("{}: {e}", audio.display()))?;
    let duration = recording.duration();
    let listener = run::ShazamListener::new(recording).map_err(|e| e.to_string())?;
    Ok((Box::new(listener), duration))
}

/// The default spacing between Shazam calls, re-exported for configs.
pub const DELAY: Duration = run::DELAY;
