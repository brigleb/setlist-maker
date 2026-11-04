//! `hindsight`: the command line. `identify` makes a set's tracklist; `chapters` arrives with
//! M4. `replay` is a hidden developer command.

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

fn main() -> ExitCode {
    match Cli::parse().command {
        Command::Identify(args) => identify::run(args),
        Command::Replay { progress, duration } => match replay::run(&progress, duration) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("hindsight: {}: {e}", progress.display());
                ExitCode::FAILURE
            }
        },
    }
}
