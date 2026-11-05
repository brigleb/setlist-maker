//! The driver against the synthetic oracle, on a fake clock: no network, no waiting.

use std::collections::VecDeque;
use std::fs;

use serde_json::json;

use super::*;
use crate::engine::oracle::{SyntheticSet, SyntheticTrack};

fn oracle() -> SyntheticSet {
    SyntheticSet::new(
        900.0,
        vec![
            SyntheticTrack::new("A", "Alpha", 0.0),
            SyntheticTrack::new("B", "Beta", 300.0),
            SyntheticTrack::new("C", "Gamma", 610.0),
        ],
        1,
    )
}

/// Answers from the oracle, except for the failures queued for given call numbers.
struct Fake {
    oracle: SyntheticSet,
    calls: usize,
    failures: VecDeque<(usize, NoAnswer)>,
    fail_from: Option<(usize, NoAnswer)>,
}

impl Fake {
    fn new() -> Fake {
        Fake {
            oracle: oracle(),
            calls: 0,
            failures: VecDeque::new(),
            fail_from: None,
        }
    }
}

impl Listener for Fake {
    fn listen(&mut self, t: f64, window: f64) -> Result<Heard, NoAnswer> {
        self.calls += 1;
        if let Some((n, why)) = &self.fail_from
            && self.calls >= *n
        {
            return Err(why.clone());
        }
        if self.failures.front().is_some_and(|(n, _)| *n == self.calls) {
            return Err(self.failures.pop_front().unwrap().1);
        }
        let (result, offsets) = self.oracle.answer(t, window);
        Ok(Heard { result, offsets })
    }
}

/// A clock that only moves when the run waits; stops after `stop_after` waits. A pause is
/// resumed at once, or ends the run when `stop_on_pause`.
#[derive(Default)]
struct FakeClock {
    elapsed: Duration,
    waits: Vec<Duration>,
    stop_after: Option<usize>,
    stopped: bool,
    pauses: usize,
    stop_on_pause: bool,
}

impl Pacing for FakeClock {
    fn wait(&mut self, d: Duration) -> bool {
        self.waits.push(d);
        self.elapsed += d;
        if self.stop_after == Some(self.waits.len()) {
            self.stopped = true;
        }
        self.stopped
    }
    fn pause(&mut self) -> bool {
        self.pauses += 1;
        self.stopped |= self.stop_on_pause;
        self.stopped
    }
    fn stopped(&self) -> bool {
        self.stopped
    }
    fn elapsed(&self) -> Duration {
        self.elapsed
    }
}

struct Set {
    _dir: tempfile::TempDir,
    paths: SetPaths,
}

fn set() -> Set {
    let dir = tempfile::tempdir().unwrap();
    let paths = SetPaths::new(&dir.path().join("set.mp3"), None);
    Set { _dir: dir, paths }
}

fn run(
    set: &Set,
    listener: &mut dyn Listener,
    clock: &mut FakeClock,
    options: &Options,
) -> Outcome {
    let resume = saved_probes(&set.paths, false).unwrap();
    identify(
        900.0,
        resume,
        listener,
        clock,
        options,
        &set.paths,
        &mut |_| {},
    )
    .unwrap()
}

fn events(set: &Set) -> Vec<Value> {
    fs::read_to_string(&set.paths.events)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn titles(outcome: &Outcome) -> Vec<String> {
    outcome
        .segments
        .iter()
        .map(|s| {
            s.info.as_ref().unwrap()["title"]
                .as_str()
                .unwrap()
                .to_string()
        })
        .collect()
}

#[test]
fn paths_sit_beside_the_audio_or_in_the_output_dir() {
    let paths = SetPaths::new(Path::new("/m/2026-09-28-Keys.mp3"), None);
    assert_eq!(
        paths.tracklist,
        Path::new("/m/2026-09-28-Keys_tracklist.md")
    );
    assert_eq!(
        paths.progress,
        Path::new("/m/2026-09-28-Keys_progress.json")
    );
    let paths = SetPaths::new(Path::new("/m/a.mp3"), Some(Path::new("/out")));
    assert_eq!(paths.events, Path::new("/out/a_events.jsonl"));
}

#[test]
fn a_run_converges_paced_and_saved() {
    let set = set();
    let (mut fake, mut clock) = (Fake::new(), FakeClock::default());
    let outcome = run(&set, &mut fake, &mut clock, &Options::default());
    assert_eq!(outcome.ended, Ended::Converged);
    assert_eq!(titles(&outcome), ["Alpha", "Beta", "Gamma"]);
    for (truth, seg) in [300.0, 610.0].iter().zip(&outcome.segments[1..]) {
        assert!((truth - seg.start).abs() <= 5.0, "{}", seg.start);
    }
    // One call per probe, and the 15s pause between them but not after the last.
    let n = outcome.probes.len();
    assert_eq!(fake.calls, n);
    assert_eq!(clock.waits, vec![DELAY; n - 1]);
    let saved = load_progress(&set.paths.progress).unwrap();
    assert_eq!(saved.probes, outcome.probes);
    assert_eq!((saved.audio_duration, saved.ended), (Some(900.0), None));
    let events = events(&set);
    assert_eq!(events.last().unwrap()["type"], "finalized");
    assert_eq!(events.last().unwrap()["reason"], "converged");
    assert!(events.iter().any(|e| e["type"] == "probe_result"));
}

#[test]
fn resume_is_replay() {
    // Stopped after 10 probes, then resumed with the same oracle: the same probes, the same
    // answers, as a run that was never stopped.
    let whole = run(
        &set(),
        &mut Fake::new(),
        &mut FakeClock::default(),
        &Options::default(),
    );

    let set = set();
    let mut fake = Fake::new();
    let mut clock = FakeClock {
        stop_after: Some(10),
        ..FakeClock::default()
    };
    let first = run(&set, &mut fake, &mut clock, &Options::default());
    assert_eq!(
        (first.ended, first.probes.len()),
        (Ended::FinishedEarly, 10)
    );
    assert_eq!(events(&set).last().unwrap()["reason"], "interrupted");
    let resumed = run(
        &set,
        &mut fake,
        &mut FakeClock::default(),
        &Options::default(),
    );
    assert_eq!(resumed.probes, whole.probes);
    assert_eq!(fake.calls, whole.probes.len());

    // A converged run resumed asks Shazam nothing.
    run(
        &set,
        &mut fake,
        &mut FakeClock::default(),
        &Options::default(),
    );
    assert_eq!(fake.calls, whole.probes.len());
}

#[test]
fn a_legacy_progress_file_resumes_and_is_rewritten_as_v2() {
    let set = set();
    let legacy = json!([[0, {"artist": "A", "title": "Alpha", "confidence": 0.9}], [30, null]]);
    fs::write(&set.paths.progress, legacy.to_string()).unwrap();
    let mut notes = Vec::new();
    let outcome = identify(
        900.0,
        saved_probes(&set.paths, false).unwrap(),
        &mut Fake::new(),
        &mut FakeClock::default(),
        &Options::default(),
        &set.paths,
        &mut |n| {
            if let Note::Resumed(k) = n {
                notes.push(k)
            }
        },
    )
    .unwrap();
    assert_eq!(notes, [2]);
    assert_eq!(outcome.probes[0].purpose, "coverage");
    let text = fs::read_to_string(&set.paths.progress).unwrap();
    assert!(text.contains("\"version\": 2"));
    // --fresh ignores it.
    assert!(saved_probes(&set.paths, true).unwrap().is_empty());
}

#[test]
fn the_budget_stops_the_run_and_it_still_finishes() {
    let set = set();
    let options = Options {
        budget: Some(Duration::from_secs(60)),
        ..Options::default()
    };
    let outcome = run(&set, &mut Fake::new(), &mut FakeClock::default(), &options);
    // 15s apart: probes at 0, 15, 30 and 45s, then 60s have passed.
    assert_eq!((outcome.ended, outcome.probes.len()), (Ended::Budget, 4));
    let events = events(&set);
    assert!(events.iter().any(|e| e["type"] == "budget_exhausted"));
    assert_eq!(events.last().unwrap()["reason"], "budget");

    let zero = Options {
        budget: Some(Duration::ZERO),
        ..Options::default()
    };
    let mut fake = Fake::new();
    let outcome = run(&self::set(), &mut fake, &mut FakeClock::default(), &zero);
    assert_eq!((outcome.ended, fake.calls), (Ended::Budget, 0));
}

#[test]
fn a_throttled_sample_is_retried_with_backoff() {
    let set = set();
    let mut fake = Fake::new();
    fake.failures = VecDeque::from([(3, NoAnswer::Throttled), (4, NoAnswer::Throttled)]);
    let mut clock = FakeClock::default();
    let mut retries = Vec::new();
    let outcome = identify(
        900.0,
        Vec::new(),
        &mut fake,
        &mut clock,
        &Options::default(),
        &set.paths,
        &mut |n| {
            if let Note::Retrying {
                wait, attempt, of, ..
            } = n
            {
                retries.push((wait.as_secs(), attempt, of));
            }
        },
    )
    .unwrap();
    assert_eq!(outcome.ended, Ended::Converged);
    assert_eq!(retries, [(20, 1, 4), (40, 2, 4)]);
    assert_eq!(&clock.waits[..4], &[DELAY, DELAY, BACKOFF[0], BACKOFF[1]]);
    // Nothing unanswered was stored.
    assert_eq!(fake.calls, outcome.probes.len() + 2);
}

#[test]
fn a_run_shazam_keeps_throttling_ends_throttled_without_storing_it() {
    let set = set();
    let mut fake = Fake::new();
    fake.fail_from = Some((6, NoAnswer::Throttled));
    let mut clock = FakeClock::default();
    let outcome = run(&set, &mut fake, &mut clock, &Options::default());
    assert_eq!(outcome.ended, Ended::Throttled);
    assert_eq!(outcome.gave_up, Some(NoAnswer::Throttled));
    assert_eq!(outcome.probes.len(), 5);
    assert_eq!(load_progress(&set.paths.progress).unwrap().probes.len(), 5);
    // About five minutes of backoff, then it gives up.
    let backoff: Duration = clock.waits[5..].iter().sum();
    assert_eq!(backoff, Duration::from_secs(300));
    assert_eq!(fake.calls, 5 + 1 + BACKOFF.len());
    assert_eq!(events(&set).last().unwrap()["reason"], "throttled");
    assert!(outcome.remaining > 0);
}

#[test]
fn other_failures_back_off_too_but_end_as_finished_early() {
    let set = set();
    let mut fake = Fake::new();
    fake.fail_from = Some((1, NoAnswer::Failed("connection refused".into())));
    let outcome = run(
        &set,
        &mut fake,
        &mut FakeClock::default(),
        &Options::default(),
    );
    assert_eq!(outcome.ended, Ended::FinishedEarly);
    assert!(outcome.probes.is_empty());
    assert_eq!(events(&set).last().unwrap()["reason"], "no_answer");
}

#[test]
fn a_stop_during_backoff_finishes_without_the_sample() {
    let set = set();
    let mut fake = Fake::new();
    fake.fail_from = Some((3, NoAnswer::Throttled));
    // Waits: two delays, then the first backoff, when the stop arrives.
    let mut clock = FakeClock {
        stop_after: Some(3),
        ..FakeClock::default()
    };
    let outcome = run(&set, &mut fake, &mut clock, &Options::default());
    assert_eq!(
        (outcome.ended, outcome.probes.len()),
        (Ended::FinishedEarly, 2)
    );
    assert_eq!(outcome.gave_up, None);
}

#[test]
fn finalize_writes_the_tracklist_then_records_how_the_run_ended() {
    let set = set();
    let outcome = run(
        &set,
        &mut Fake::new(),
        &mut FakeClock::default(),
        &Options::default(),
    );
    let dir = tempfile::tempdir().unwrap();
    let mut db = CorrectionsDb::open(dir.path().join("corrections.json"));
    db.add_correction("B", "Beta", "Bee", "Beta (Live)");
    let tracklist = segments_to_tracklist(
        &outcome.segments,
        "set.mp3",
        Some(&db),
        "2026-11-04 09:00".into(),
    );
    let beta = &tracklist.tracks[1];
    assert_eq!(
        (beta.artist.as_str(), beta.title.as_str()),
        ("Bee", "Beta (Live)")
    );
    assert_eq!(beta.original_artist.as_deref(), Some("B"));
    assert_eq!(tracklist.tracks[0].original_artist, None);

    finalize(&tracklist, &outcome, &set.paths).unwrap();
    let md = fs::read(&set.paths.tracklist).unwrap();
    assert!(String::from_utf8_lossy(&md).contains("**Bee** - Beta (Live)"));
    let saved = load_progress(&set.paths.progress).unwrap();
    assert_eq!(saved.ended, Some(Ended::Converged));
    let sha = crate::sha256_hex(&md);
    assert_eq!(saved.tracklist_sha256, Some(sha));
    assert_eq!(saved.probes, outcome.probes);
}

#[test]
fn a_finished_tracklist_is_reused_and_an_unfinished_run_carries_on() {
    let set = set();
    assert_eq!(existing(&set.paths), Existing::Nothing);
    let finish = |options: &Options| {
        let outcome = run(&set, &mut Fake::new(), &mut FakeClock::default(), options);
        let tl = segments_to_tracklist(&outcome.segments, "set.mp3", None, "now".into());
        finalize(&tl, &outcome, &set.paths).unwrap();
        outcome
    };
    let budget = Options {
        budget: Some(Duration::from_secs(60)),
        ..Options::default()
    };
    let stopped = finish(&budget);
    assert_eq!(
        existing(&set.paths),
        Existing::Unfinished {
            ended: Ended::Budget,
            edited: false
        }
    );
    // Carrying on resumes the saved probes rather than starting over.
    let resumed = finish(&Options::default());
    assert_eq!(resumed.probes[..4], stopped.probes[..]);
    assert!(matches!(existing(&set.paths), Existing::Finished(_)));

    // Stopped again, then edited by hand: carrying on would lose the edit.
    fs::remove_file(&set.paths.progress).unwrap();
    finish(&budget);
    let mut md = fs::read_to_string(&set.paths.tracklist).unwrap();
    md = md.replace("Alpha", "Alpha (corrected)");
    fs::write(&set.paths.tracklist, md).unwrap();
    assert_eq!(
        existing(&set.paths),
        Existing::Unfinished {
            ended: Ended::Budget,
            edited: true
        }
    );

    // A Python run records no `ended`: it finished.
    fs::write(
        &set.paths.progress,
        r#"{"version": 2, "audio_duration": 900, "probes": []}"#,
    )
    .unwrap();
    assert!(matches!(existing(&set.paths), Existing::Finished(_)));
}

#[test]
fn an_unidentified_segment_is_a_blank_row() {
    let segments = vec![Segment {
        start: 12.5,
        info: None,
        confidence: crate::engine::boundary::Confidence::Resolved,
    }];
    let tl = segments_to_tracklist(&segments, "x.mp3", None, "now".into());
    // Python's round(): half to even.
    assert_eq!(tl.tracks[0].timestamp, 12);
    assert!(tl.tracks[0].is_unidentified());
}

#[test]
fn probe_lines() {
    let probe = |purpose: &str, result| Probe {
        t: 310.0,
        window: 12.0,
        purpose: purpose.into(),
        result,
        offsets: None,
    };
    let hit = probe(
        "refine",
        Some(json!({"artist": "B", "title": "Beta", "confidence": 0.9})),
    );
    assert_eq!(probe_line(&hit), "  >    5:10  +   90%  B - Beta");
    assert_eq!(
        probe_line(&probe("coverage", None)),
        "  .    5:10  -  not identified"
    );
    let unsure = probe("manual", Some(json!({"artist": "B", "title": "Beta"})));
    assert_eq!(probe_line(&unsure), "  *    5:10  +   --   B - Beta");
}

fn throttled_for(calls: std::ops::RangeInclusive<usize>) -> Fake {
    let mut fake = Fake::new();
    fake.failures = calls.map(|n| (n, NoAnswer::Throttled)).collect();
    fake
}

fn pausing() -> Options {
    Options {
        pause_when_throttled: true,
        ..Options::default()
    }
}

#[test]
fn a_run_that_pauses_when_throttled_carries_on_once_resumed() {
    // Call 3 and its four retries are throttled: about five minutes, then a pause.
    let plain = run(
        &set(),
        &mut Fake::new(),
        &mut FakeClock::default(),
        &Options::default(),
    );
    let set = set();
    let mut fake = throttled_for(3..=7);
    let mut clock = FakeClock::default();
    let mut notes = Vec::new();
    let outcome = identify(
        900.0,
        Vec::new(),
        &mut fake,
        &mut clock,
        &pausing(),
        &set.paths,
        &mut |n| match n {
            Note::Paused(why) => notes.push(format!("paused: {why}")),
            Note::Unpaused => notes.push("unpaused".into()),
            _ => {}
        },
    )
    .unwrap();
    assert_eq!(clock.pauses, 1);
    assert_eq!(
        notes,
        ["paused: Shazam is throttling (HTTP 429)", "unpaused"]
    );
    // After the pause it asks about the same window again, with a fresh backoff.
    assert_eq!(outcome.ended, Ended::Converged);
    assert_eq!(fake.calls, outcome.probes.len() + 5);
    assert_eq!(outcome.probes, plain.probes);
}

#[test]
fn a_stop_while_paused_finishes_early_with_what_was_heard() {
    let set = set();
    let mut clock = FakeClock {
        stop_on_pause: true,
        ..FakeClock::default()
    };
    let outcome = run(&set, &mut throttled_for(3..=7), &mut clock, &pausing());
    assert_eq!(
        (outcome.ended, outcome.probes.len()),
        (Ended::FinishedEarly, 2)
    );
    assert_eq!(events(&set).last().unwrap()["reason"], "interrupted");
}

#[test]
fn a_question_is_answered_ahead_of_the_engine_and_kept_as_evidence() {
    let set = set();
    let questions = Questions::new();
    let asked = questions.submit(450.0);
    let options = Options {
        questions: Some(questions.clone()),
        ..Options::default()
    };
    let mut fake = Fake::new();
    let outcome = run(&set, &mut fake, &mut FakeClock::default(), &options);
    let probe = asked.wait(Duration::ZERO).unwrap();
    // Centred on the moment asked about, the way the editor samples.
    assert_eq!(
        (probe.t, probe.window, probe.purpose.as_str()),
        (435.0, 30.0, MANUAL)
    );
    assert_eq!(outcome.probes[0], probe);
    assert_eq!(load_progress(&set.paths.progress).unwrap().probes[0], probe);
    // Resume is replay: the saved run, manual probe included, folds to the same answer.
    let again = run(
        &set,
        &mut Fake::new(),
        &mut FakeClock::default(),
        &Options::default(),
    );
    assert_eq!(again.segments, outcome.segments);
    // The run is over: nothing will serve another question.
    let late = questions.submit(100.0);
    assert!(late.wait(Duration::ZERO).is_err());
}

#[test]
fn a_question_shazam_wont_answer_fails_and_stores_nothing() {
    let set = set();
    let questions = Questions::new();
    let asked = questions.submit(450.0);
    let options = Options {
        questions: Some(questions.clone()),
        ..pausing()
    };
    let mut fake = throttled_for(1..=5);
    let outcome = run(&set, &mut fake, &mut FakeClock::default(), &options);
    let why = asked.wait(Duration::ZERO).unwrap_err();
    assert!(why.contains("throttling"), "{why}");
    assert!(outcome.probes.iter().all(|p| p.purpose != MANUAL));
    assert_eq!(outcome.ended, Ended::Converged);
}

#[test]
fn a_question_waits_for_its_answer_on_another_thread() {
    let questions = Questions::new();
    let asker = {
        let q = questions.clone();
        std::thread::spawn(move || q.ask(100.0, Duration::from_secs(5)))
    };
    let taken = loop {
        if let Some(q) = questions.pop() {
            break q;
        }
        std::thread::yield_now();
    };
    let probe = Probe {
        t: 85.0,
        window: 30.0,
        purpose: MANUAL.into(),
        result: None,
        offsets: None,
    };
    taken.answer(Ok(probe.clone()));
    assert_eq!(asker.join().unwrap(), Ok(probe));
    // Unanswered in time: the asker gets an error and the question leaves the queue.
    assert!(questions.ask(5.0, Duration::from_millis(10)).is_err());
    assert!(!questions.pending());
}
