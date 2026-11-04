//! `resave tracklist <x_tracklist.md>...`: load each set and save it again, in place.
//!
//! Hindsight's half of M1's golden check (`scripts/golden_m1.py` runs Python's half and
//! compares). Run it on copies: it rewrites the files it's given.

use std::path::Path;
use std::process::ExitCode;

use hindsight_core::model::{load_tracklist, save_tracklist};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some((kind, paths)) = args.split_first() else {
        eprintln!("usage: resave tracklist <file>...");
        return ExitCode::from(2);
    };
    let mut failed = false;
    for path in paths.iter().map(Path::new) {
        let result = match kind.as_str() {
            "tracklist" => load_tracklist(path).and_then(|parsed| {
                for w in &parsed.warnings {
                    eprintln!("{}: warning: {w}", path.display());
                }
                save_tracklist(&parsed.tracklist, path, None)
            }),
            other => {
                eprintln!("unknown kind {other:?}");
                return ExitCode::from(2);
            }
        };
        if let Err(e) = result {
            eprintln!("{}: {e}", path.display());
            failed = true;
        }
    }
    if failed {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}
