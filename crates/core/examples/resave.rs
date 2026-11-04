//! Loads files and saves them again, in place, the way the app would:
//!
//! - `resave tracklist <x_tracklist.md>...`: the markdown and its sidecar
//! - `resave progress <x_progress.json>...`: the progress file (a legacy one comes back v2)
//! - `resave episode <x_tracklist.md>...`: the episode tags, saved or defaulted, which also
//!   remembers the artist in `$XDG_CONFIG_HOME/hindsight/config.json`
//!
//! Hindsight's half of M1's golden check (`scripts/golden_m1.py` runs Python's half and
//! compares). Run it on copies: it rewrites the files it's given.

use std::path::Path;
use std::process::ExitCode;

use hindsight_core::model::{
    episode_tags, load_progress, load_tracklist, save_episode_tags, save_progress, save_tracklist,
};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some((kind, paths)) = args.split_first() else {
        eprintln!("usage: resave tracklist|progress|episode <file>...");
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
            "progress" => load_progress(path).and_then(|p| save_progress(path, &p)),
            "episode" => {
                let tags = episode_tags(path);
                save_episode_tags(path, &tags.title, &tags.artist)
            }
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
