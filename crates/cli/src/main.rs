//! `hindsight`: the command line. `identify` makes a set's tracklist; `chapters` embeds it in
//! the MP3. `replay` is a hidden developer command.

mod chapters;
mod identify;
mod pyjson;
mod replay;

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "hindsight", version, about = "Identify the tracks in a DJ set")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Identify the tracks in a recording and write <set>_tracklist.md beside it.
    ///
    /// Samples adaptively, one Shazam lookup every 15 seconds, and saves after each, so
    /// Ctrl-C finishes early with what was heard (twice aborts) and running it again resumes.
    Identify(identify::Args),
    /// Embed a tracklist in its MP3 as chapter markers, with each track's cover art.
    ///
    /// Writes ID3v2.3 chapters podcast players list, the episode's cover, title and artist
    /// (as saved in the editor), and leaves the audio as it was.
    Chapters(chapters::Args),
    /// Fold a progress file one probe at a time, printing a JSON line of what the engine
    /// says after each (and before the first). M2's parity harness compares it with Python.
    #[command(hide = true)]
    Replay {
        /// A `<set>_progress.json`.
        progress: PathBuf,
        /// The recording's length in seconds, for a file that doesn't record it.
        #[arg(long)]
        duration: Option<f64>,
    },
}

/// The first launch copies what Python learned: its corrections and episode artist, and its
/// artwork and peaks caches. A copy: Python keeps working until the cutover.
fn adopt_python_folders() {
    use hindsight_core::model::fileio::{
        adopt_python_cache, adopt_python_config, cache_dir, config_dir,
    };
    let warn = |base: &std::path::Path, e: std::io::Error| {
        eprintln!(
            "Warning: couldn't copy setlist-maker's {}: {e}",
            base.display()
        )
    };
    if let Some(base) = config_dir().parent()
        && let Err(e) = adopt_python_config(base)
    {
        warn(base, e);
    }
    if let Some(base) = cache_dir().parent()
        && let Err(e) = adopt_python_cache(base)
    {
        warn(base, e);
    }
}

/// Library warnings (a corrupt corrections file set aside, an unreadable episode file) on
/// stderr; nothing quieter.
struct Warnings;

impl log::Log for Warnings {
    fn enabled(&self, m: &log::Metadata) -> bool {
        m.level() <= log::Level::Warn
    }
    fn log(&self, r: &log::Record) {
        if self.enabled(r.metadata()) {
            eprintln!("Warning: {}", r.args());
        }
    }
    fn flush(&self) {}
}

fn main() -> ExitCode {
    if log::set_logger(&Warnings).is_ok() {
        log::set_max_level(log::LevelFilter::Warn);
    }
    match Cli::parse().command {
        Command::Identify(args) => identify::run(args),
        Command::Chapters(args) => chapters::run(args),
        Command::Replay { progress, duration } => match replay::run(&progress, duration) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("hindsight: {}: {e}", progress.display());
                ExitCode::FAILURE
            }
        },
    }
}
