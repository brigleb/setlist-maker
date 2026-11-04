//! `hindsight chapters <tracklist>`: embed the tracklist in its MP3 as chapter markers, with
//! each track's cover and the episode's cover, title and artist.

use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;

use hindsight_core::artwork::image::chapter_jpeg;
use hindsight_core::artwork::{ArtworkCache, Web, cache};
use hindsight_core::audio::undecoded_seconds;
use hindsight_core::chapters::{self, Art, CoverFrom, Progress, Written};
use hindsight_core::model::episode::episode_tags;
use hindsight_core::model::store::{find_audio_file, load_tracklist};

#[derive(clap::Args)]
pub struct Args {
    /// The set's `<set>_tracklist.md`.
    pub tracklist: PathBuf,
    /// The MP3, when it isn't beside the tracklist with the same name.
    #[arg(long, value_name = "MP3")]
    pub audio: Option<PathBuf>,
    /// An image for the episode cover, in place of the one the set would get.
    #[arg(long, value_name = "IMAGE")]
    pub cover: Option<PathBuf>,
    /// Embed the chapters without looking up each track's cover.
    #[arg(long)]
    pub no_artwork: bool,
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("Error: {message}");
    ExitCode::FAILURE
}

pub fn run(args: Args) -> ExitCode {
    let path = &args.tracklist;
    if !path.is_file() {
        return fail(format!("tracklist not found: {}", path.display()));
    }
    println!("Loading tracklist: {}", path.display());
    let parsed = match load_tracklist(path) {
        Ok(p) => p,
        Err(e) => return fail(format!("can't read {}: {e}", path.display())),
    };
    for warning in &parsed.warnings {
        eprintln!("Warning: {warning}");
    }
    let tracklist = parsed.tracklist;
    let kept = tracklist.tracks.iter().filter(|t| !t.rejected);
    if kept.clone().all(|t| t.is_unidentified()) {
        return fail("no identified tracks in the tracklist");
    }
    println!("  Found {} tracks", kept.count());

    let Some(audio) = args.audio.clone().or_else(|| find_audio_file(path)) else {
        return fail("can't find the audio file; pass --audio with the MP3's path");
    };
    if !audio.is_file() {
        return fail(format!("audio file not found: {}", audio.display()));
    }
    if !audio
        .extension()
        .is_some_and(|e| e.eq_ignore_ascii_case("mp3"))
    {
        return fail(format!(
            "chapter markers need an MP3, not {}",
            audio.display()
        ));
    }
    println!("  Audio file: {}", audio.display());

    // Checked before anything is looked up or written.
    let cover = match &args.cover {
        None => None,
        Some(p) => match fs::read(p).ok().as_deref().and_then(chapter_jpeg) {
            Some(jpeg) => Some(jpeg),
            None => return fail(format!("can't read the cover image {}", p.display())),
        },
    };
    crate::adopt_python_folders();

    let seconds = match undecoded_seconds(&audio) {
        Ok(s) => s,
        Err(e) => return fail(e),
    };
    let duration_ms = (seconds * 1000.0) as u32;
    let Some(web) = Web::new() else {
        return fail("can't start an HTTP client");
    };
    let artwork = ArtworkCache::new(cache::default_dir(), web);
    let tags = episode_tags(path);

    if !args.no_artwork {
        println!("\nFetching artwork...");
    }
    let prepared = chapters::prepare(
        &tracklist,
        path,
        duration_ms,
        &tags,
        cover,
        !args.no_artwork,
        &artwork,
        &mut |p| match p {
            Progress::LookingUp { index, of, track } => print!(
                "  [{}/{of}] {} - {}",
                index + 1,
                track.time_str(),
                chapters::chapter_title(track)
            ),
            Progress::Looked(art) => println!(
                "{}",
                match art {
                    Art::Found => "",
                    Art::NotFound => " (no artwork found)",
                    Art::UploadMissing => " (its uploaded artwork is missing; not synced yet?)",
                    Art::Unidentified => " (unidentified, skipped)",
                }
            ),
            Progress::SavedCoverUnreadable => {
                eprintln!("  Warning: ignoring the saved episode cover: not an image")
            }
        },
    );
    if !args.no_artwork {
        println!("  {} chapter image(s)", prepared.images());
    }

    println!("\nEmbedding chapter markers...");
    let episode = &prepared.episode;
    let names: Vec<&str> = [&episode.artist, &episode.title]
        .into_iter()
        .flatten()
        .map(String::as_str)
        .collect();
    if !names.is_empty() {
        println!("  Episode: {}", names.join(" - "));
    }
    let written = match chapters::embed(&audio, episode, &prepared.chapters) {
        Ok(w) => w,
        Err(e) => return fail(format!("{}: {e}", audio.display())),
    };
    println!(
        "  Embedded {} chapter(s) into {}",
        prepared.chapters.len(),
        audio.display()
    );
    if prepared.images() > 0 {
        println!("  Embedded {} chapter image(s)", prepared.images());
    }
    if let Some(from) = prepared.cover_from {
        let from = match from {
            CoverFrom::Given => "from --cover".to_string(),
            CoverFrom::SavedFile => "the set's saved cover".to_string(),
            CoverFrom::Track(i) => format!("{}'s artwork", prepared.chapters[i].title),
        };
        println!("  Embedded the episode cover ({from})");
    }
    if written == Written::Rewritten {
        println!("  (The tag outgrew its room, so the MP3 was rewritten with space to spare.)");
    }
    ExitCode::SUCCESS
}
