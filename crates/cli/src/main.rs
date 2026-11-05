//! `hindsight`: the command line. `identify` makes a set's tracklist; `chapters` embeds it in
//! the MP3. `replay` and `serve` are hidden developer commands.

mod chapters;
mod identify;
mod pyjson;
mod replay;
mod serve;

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
    /// The app's editor on a loopback port, for headless browser checks.
    #[command(hide = true)]
    Serve(serve::Args),
}

/// The first launch copies what Python learned: its corrections and episode artist, and its
/// artwork and peaks caches. A copy: Python keeps working until the cutover.
fn adopt_python_folders() {
    for (base, e) in hindsight_core::model::fileio::adopt_python_folders() {
        eprintln!(
            "Warning: couldn't copy setlist-maker's {}: {e}",
            base.display()
        );
    }
}

/// `program` on this process's PATH, if it's an executable file.
pub(crate) fn find_on_path(program: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    std::env::split_paths(&std::env::var_os("PATH")?)
        .map(|dir| dir.join(program))
        .find(|p| {
            p.metadata()
                .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        })
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
        Command::Serve(args) => serve::run(args),
        Command::Replay { progress, duration } => match replay::run(&progress, duration) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("hindsight: {}: {e}", progress.display());
                ExitCode::FAILURE
            }
        },
    }
}
