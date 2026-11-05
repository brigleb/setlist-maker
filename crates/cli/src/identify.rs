//! `hindsight identify <audio>`: sample the set adaptively, then write `<set>_tracklist.md`
//! and its sidecar. Plain lines, one per sample; the progress file is saved after each, so
//! Ctrl-C (or a crash) loses nothing and running the command again resumes.

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use hindsight_core::audio::{AudioError, Recording, format_timestamp};
use hindsight_core::engine::boundary::Confidence;
use hindsight_core::model::CorrectionsDb;
use hindsight_core::model::progress::Ended;
use hindsight_core::run::{
    self, Clock, Existing, Note, Options, Outcome, SetPaths, ShazamListener, StopSignal,
};
use hindsight_core::summary;

/// What symphonia is built to decode here.
const EXTENSIONS: [&str; 4] = [".mp3", ".m4a", ".wav", ".flac"];

#[derive(clap::Args)]
pub struct Args {
    /// The recording (MP3, M4A, WAV or FLAC).
    pub audio: PathBuf,
    /// Write the tracklist and run files here instead of beside the audio.
    #[arg(short, long, value_name = "DIR")]
    pub output_dir: Option<PathBuf>,
    /// Stop after about this long and write what was found: 45m, 2h, 90s, or minutes.
    #[arg(long, value_name = "DURATION", value_parser = parse_budget)]
    pub budget: Option<Duration>,
    /// Identify a file that decodes shorter than it says it is (still syncing?).
    #[arg(long)]
    pub allow_partial: bool,
    /// Ask the `claude` CLI for a one-paragraph description of the set.
    #[arg(long)]
    pub summary: bool,
    /// Ignore an existing tracklist and progress file and start over.
    #[arg(long)]
    pub fresh: bool,
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("Error: {message}");
    ExitCode::FAILURE
}

/// `2h`, `45m`, `90s`, or a bare number of minutes.
pub fn parse_budget(text: &str) -> Result<Duration, String> {
    let t = text.trim().to_ascii_lowercase();
    let (number, unit) = match t.char_indices().last() {
        Some((i, c @ ('h' | 'm' | 's'))) => (t[..i].trim_end(), c),
        _ => (t.as_str(), 'm'),
    };
    let valid = !number.is_empty()
        && number.chars().all(|c| c.is_ascii_digit() || c == '.')
        && number.starts_with(|c: char| c.is_ascii_digit())
        && number.matches('.').count() <= 1
        && !number.ends_with('.');
    let value: f64 = match number.parse() {
        Ok(v) if valid => v,
        _ => return Err(format!("invalid duration: {text:?} (try 45m, 2h or 90s)")),
    };
    let scale = match unit {
        'h' => 3600.0,
        's' => 1.0,
        _ => 60.0,
    };
    Ok(Duration::from_secs_f64(value * scale))
}

pub fn run(args: Args) -> ExitCode {
    let audio = &args.audio;
    let ext = audio
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))
        .unwrap_or_default();
    if !audio.is_file() {
        return fail(format!("not a file: {}", audio.display()));
    }
    if !EXTENSIONS.contains(&ext.as_str()) {
        return fail(format!(
            "not a supported audio file: {} (supported: {})",
            audio.display(),
            EXTENSIONS.join(", ")
        ));
    }
    crate::adopt_python_folders();
    if let Some(dir) = &args.output_dir
        && let Err(e) = std::fs::create_dir_all(dir)
    {
        return fail(format!("can't create {}: {e}", dir.display()));
    }
    let paths = SetPaths::new(audio, args.output_dir.as_deref());

    match run::existing(&paths) {
        _ if args.fresh => {}
        Existing::Nothing => {}
        Existing::Finished(tracklist) => {
            println!(
                "Found existing tracklist: {} (use --fresh to identify again)",
                paths.tracklist.display()
            );
            println!("\n{}", tracklist.to_markdown());
            return ExitCode::SUCCESS;
        }
        Existing::Unfinished { edited: true, .. } => {
            return fail(format!(
                "{} was edited since its run stopped early, and carrying on would replace \
                 those edits. Move it aside to carry on, or pass --fresh to start over.",
                paths.tracklist.display()
            ));
        }
        Existing::Unfinished { ended, .. } => {
            let why = match ended {
                Ended::Throttled => "Shazam was throttling",
                Ended::Budget => "its budget ran out",
                _ => "it was stopped",
            };
            println!("Carrying on with the run that stopped early ({why}).");
        }
    }

    println!("Loading audio: {}", audio.display());
    let recording = match Recording::open(audio, args.allow_partial) {
        Ok(r) => r,
        Err(e @ AudioError::Truncated { .. }) => {
            return fail(format!("{e} Or pass --allow-partial to use what decoded."));
        }
        Err(e) => return fail(e),
    };
    let duration = recording.duration();
    println!(
        "  Duration: {} ({duration} seconds)",
        format_timestamp(duration as i64)
    );
    let resume = match run::saved_probes(&paths, args.fresh) {
        Ok(p) => p,
        Err(e) => return fail(format!("can't read {}: {e}", paths.progress.display())),
    };
    let mut listener = match ShazamListener::new(recording) {
        Ok(l) => l,
        Err(e) => return fail(e),
    };

    let stop = StopSignal::new();
    let presses = Arc::new(AtomicUsize::new(0));
    let (handler_stop, handler_presses) = (stop.clone(), presses.clone());
    let installed = ctrlc::set_handler(move || {
        if handler_presses.fetch_add(1, Ordering::SeqCst) == 0 {
            eprintln!("\n  Stopping after this sample... (Ctrl-C again to abort)");
            handler_stop.request();
        } else {
            // Every answer is already saved, so aborting loses nothing.
            std::process::exit(130);
        }
    });
    if let Err(e) = installed {
        eprintln!("Warning: Ctrl-C will abort rather than finish early ({e})");
    }

    let options = Options {
        budget: args.budget,
        ..Options::default()
    };
    let mut clock = Clock::new(stop);
    let outcome = run::identify(
        duration,
        resume,
        &mut listener,
        &mut clock,
        &options,
        &paths,
        &mut |note| match note {
            Note::Resumed(n) => println!("  Resuming with {n} previous samples"),
            Note::Probe(p) => println!("{}", run::probe_line(p)),
            Note::Retrying {
                why,
                wait,
                attempt,
                of,
            } => println!(
                "  {why}; asking again in {}s ({attempt} of {of})",
                wait.as_secs()
            ),
            // The CLI finishes rather than pausing.
            Note::Paused(_) | Note::Unpaused => {}
        },
    );
    let outcome = match outcome {
        Ok(o) => o,
        Err(e) => return fail(e),
    };
    finish(&args, &paths, &outcome)
}

fn finish(args: &Args, paths: &SetPaths, outcome: &Outcome) -> ExitCode {
    println!("\n  Generating tracklist...");
    let corrections = CorrectionsDb::open_default();
    let source = args.audio.file_name().unwrap_or_default().to_string_lossy();
    let mut tracklist = run::segments_to_tracklist(
        &outcome.segments,
        &source,
        Some(&corrections),
        run::now_stamp(),
    );
    if args.summary {
        let lines: Vec<String> = tracklist
            .tracks
            .iter()
            .filter(|t| !t.rejected && !t.is_unidentified())
            .map(|t| format!("{} - {}", t.artist, t.title))
            .collect();
        println!("  Writing the set's description...");
        match crate::find_on_path("claude") {
            None => eprintln!("  Warning: 'claude' CLI not found; no description."),
            Some(claude) => match summary::generate(&claude, &lines) {
                Ok(s) => tracklist.summary = Some(s),
                Err(why) => eprintln!("  Warning: no description: {why}"),
            },
        }
    }
    if let Err(e) = run::finalize(&tracklist, outcome, paths) {
        return fail(format!("can't save {}: {e}", paths.tracklist.display()));
    }
    println!("  Saved: {}", paths.tracklist.display());
    println!("  Found {} tracks", tracklist.tracks.len());
    println!("\n{}", "─".repeat(40));
    println!("{}", tracklist.to_markdown());

    match (&outcome.ended, &outcome.gave_up) {
        (Ended::Throttled, _) | (_, Some(_)) => {
            let why = outcome
                .gave_up
                .as_ref()
                .map(|w| w.to_string())
                .unwrap_or_default();
            eprintln!(
                "\n{why}, so the run stopped early after {} samples, with about {} still to \
                 go.{} The tracklist has what was heard. Run the same command again later to \
                 resume.",
                outcome.probes.len(),
                outcome.remaining,
                coarse_note(outcome)
            );
            ExitCode::FAILURE
        }
        _ => ExitCode::SUCCESS,
    }
}

/// Where the tracklist is still a rough guess.
fn coarse_note(outcome: &Outcome) -> String {
    let coarse: Vec<String> = outcome
        .segments
        .iter()
        .skip(1)
        .filter(|s| s.confidence == Confidence::Coarse)
        .map(|s| format_timestamp(s.start as i64))
        .collect();
    match coarse.len() {
        0 => String::new(),
        n if n <= 6 => format!(" Still rough: the starts at {}.", coarse.join(", ")),
        n => format!(" Still rough: {n} starts, from {} on.", coarse[0]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budgets() {
        // Python's test_cli_adaptive cases.
        let secs = |t| parse_budget(t).map(|d| d.as_secs_f64());
        assert_eq!(secs("2h"), Ok(7200.0));
        assert_eq!(secs("45m"), Ok(2700.0));
        assert_eq!(secs("90s"), Ok(90.0));
        assert_eq!(secs("30"), Ok(1800.0));
        assert_eq!(secs(" 1.5 H "), Ok(5400.0));
        for bad in ["", "h", "2x", "-5m", ".5m", "1.m", "1..2"] {
            assert!(parse_budget(bad).is_err(), "{bad:?}");
        }
    }
}
