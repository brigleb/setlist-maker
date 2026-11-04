//! What a tracklist puts in the MP3: one chapter per kept track, its cover, and the episode's.
//! Python's `embed_chapters_for_tracklist`, less the printing, which the caller does from
//! [`Progress`].

use std::fs;
use std::path::Path;

use super::{Chapter, Episode};
use crate::artwork::image::chapter_jpeg;
use crate::artwork::{ArtworkCache, Fetch};
use crate::model::episode::EpisodeTags;
use crate::model::track::{Track, Tracklist};
use crate::model::uploads::{artwork_dir_for, cover_path_for, is_upload_ref};

/// What one track's chapter got.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Art {
    /// A cover, found or uploaded.
    Found,
    /// No source has one, or it doesn't decode: no picture, no APIC.
    NotFound,
    /// Its uploaded image isn't in the set's `_artwork/` folder (not synced yet?).
    UploadMissing,
    /// Unidentified: a chapter, but nothing to look up.
    Unidentified,
}

/// Where the episode cover came from, in precedence order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CoverFrom {
    /// `--cover`.
    Given,
    /// `<set>_cover.jpg`, uploaded in the editor.
    SavedFile,
    /// This chapter's track: the starred one, else the first with art.
    Track(usize),
}

pub enum Progress<'a> {
    /// Before the artwork lookup for chapter `index` of `of`.
    LookingUp {
        index: usize,
        of: usize,
        track: &'a Track,
    },
    Looked(Art),
    /// `<set>_cover.jpg` is there but isn't an image.
    SavedCoverUnreadable,
}

pub struct Prepared {
    pub chapters: Vec<Chapter>,
    pub episode: Episode,
    pub cover_from: Option<CoverFrom>,
}

impl Prepared {
    pub fn images(&self) -> usize {
        self.chapters.iter().filter(|c| c.image.is_some()).count()
    }
}

/// A chapter's title: whichever of artist and title the track has, joined, so a row that
/// knows only its title (#44) isn't " - Title".
pub fn chapter_title(track: &Track) -> String {
    let parts: Vec<&str> = [track.artist.trim(), track.title.trim()]
        .into_iter()
        .filter(|p| !p.is_empty())
        .collect();
    if parts.is_empty() {
        "Unknown Track".into()
    } else {
        parts.join(" - ")
    }
}

fn ms(seconds: i64) -> u32 {
    u32::try_from(seconds.max(0).saturating_mul(1000)).unwrap_or(u32::MAX)
}

/// The chapters and episode tags for a tracklist: every kept track in time order (unidentified
/// ones too, with no picture), each ending where the next starts and the last at `duration_ms`.
///
/// With `fetch_art`, each identified track's cover is looked up through `artwork` (uploads
/// from the set's `_artwork/` folder). The episode cover is, in order: `cover` (from
/// `--cover`, already a chapter picture), `<set>_cover.jpg`, the starred track's picture, the
/// first track with one. The first two are used with or without `fetch_art`.
#[allow(clippy::too_many_arguments)] // separate inputs the CLI and the editor each supply
pub fn prepare<F: Fetch>(
    tracklist: &Tracklist,
    tracklist_path: &Path,
    duration_ms: u32,
    tags: &EpisodeTags,
    cover: Option<Vec<u8>>,
    fetch_art: bool,
    artwork: &ArtworkCache<F>,
    progress: &mut dyn FnMut(Progress),
) -> Prepared {
    let mut kept: Vec<&Track> = tracklist.tracks.iter().filter(|t| !t.rejected).collect();
    // In time order whatever order the file lists them in: the CHAPs go in as added (#33), and
    // each chapter ends where the next starts.
    kept.sort_by_key(|t| t.timestamp);
    let uploads = artwork_dir_for(tracklist_path);

    let mut cover_from = cover.as_ref().map(|_| CoverFrom::Given);
    let mut cover = cover;
    if cover.is_none() {
        let saved = cover_path_for(tracklist_path);
        if let Ok(data) = fs::read(&saved) {
            match chapter_jpeg(&data) {
                Some(jpeg) => (cover, cover_from) = (Some(jpeg), Some(CoverFrom::SavedFile)),
                None => progress(Progress::SavedCoverUnreadable),
            }
        }
    }

    let mut chapters: Vec<Chapter> = kept
        .iter()
        .enumerate()
        .map(|(i, t)| Chapter {
            start_ms: ms(t.timestamp),
            end_ms: kept
                .get(i + 1)
                .map_or(duration_ms, |next| ms(next.timestamp)),
            title: chapter_title(t),
            image: None,
        })
        .collect();

    if fetch_art {
        for (i, track) in kept.iter().enumerate() {
            progress(Progress::LookingUp {
                index: i,
                of: kept.len(),
                track,
            });
            let url = track.coverart_url.as_deref();
            let art = if track.is_unidentified() {
                Art::Unidentified
            } else {
                let src = artwork.source_artwork(&track.artist, &track.title, url, Some(&uploads));
                chapters[i].image = src.as_deref().and_then(chapter_jpeg);
                match (&chapters[i].image, &src) {
                    (Some(_), _) => Art::Found,
                    (None, None) if is_upload_ref(url) => Art::UploadMissing,
                    _ => Art::NotFound,
                }
            };
            progress(Progress::Looked(art));
        }
        if cover.is_none() {
            let identified = |&i: &usize| !kept[i].is_unidentified();
            let starred = (0..kept.len())
                .filter(identified)
                .find(|&i| kept[i].is_episode_cover);
            let order = starred
                .into_iter()
                .chain((0..kept.len()).filter(|&i| Some(i) != starred));
            if let Some(i) = order
                .filter(identified)
                .find(|&i| chapters[i].image.is_some())
            {
                (cover, cover_from) = (chapters[i].image.clone(), Some(CoverFrom::Track(i)));
            }
        }
    }

    let filled = |s: &str| Some(s.trim().to_string()).filter(|s| !s.is_empty());
    Prepared {
        chapters,
        episode: Episode {
            title: filled(&tags.title),
            artist: filled(&tags.artist),
            cover,
        },
        cover_from,
    }
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use image::{ImageFormat, Rgb, RgbImage};

    use super::*;
    use crate::artwork::cache::cache_key;
    use crate::artwork::fetch::fake::Canned;

    fn png(color: [u8; 3]) -> Vec<u8> {
        let mut out = Vec::new();
        RgbImage::from_pixel(40, 40, Rgb(color))
            .write_to(&mut Cursor::new(&mut out), ImageFormat::Png)
            .unwrap();
        out
    }

    fn track(t: i64, artist: &str, title: &str) -> Track {
        Track::new(t, artist, title)
    }

    struct Set {
        _dir: tempfile::TempDir,
        path: std::path::PathBuf,
        cache: ArtworkCache<Canned>,
    }

    /// A set folder, and a cache that already knows `with_art`'s covers (by colour) and that
    /// everything else has none, so nothing is fetched.
    fn set(tracks: &[Track], with_art: &[(&str, [u8; 3])]) -> Set {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("2026-09-28-Keys_tracklist.md");
        let cache_dir = dir.path().join("cache");
        fs::create_dir(&cache_dir).unwrap();
        for t in tracks {
            let key = cache_key(&t.artist, &t.title, t.coverart_url.as_deref(), 600);
            match with_art.iter().find(|(a, _)| *a == t.artist) {
                Some((_, c)) => fs::write(cache_dir.join(format!("{key}.src")), png(*c)).unwrap(),
                None => fs::write(cache_dir.join(format!("{key}.fallback")), "").unwrap(),
            }
        }
        Set {
            _dir: dir,
            path,
            cache: ArtworkCache::new(cache_dir, Canned::default()),
        }
    }

    fn run(s: &Set, tracks: Vec<Track>, cover: Option<Vec<u8>>, fetch_art: bool) -> Prepared {
        let tl = Tracklist {
            source_file: "2026-09-28-Keys.mp3".into(),
            tracks,
            generated_on: None,
            summary: None,
        };
        let tags = EpisodeTags {
            title: "September 28, 2026".into(),
            artist: " ".into(),
        };
        prepare(
            &tl,
            &s.path,
            600_000,
            &tags,
            cover,
            fetch_art,
            &s.cache,
            &mut |_| {},
        )
    }

    fn colour(jpeg: &[u8]) -> [u8; 3] {
        image::load_from_memory(jpeg)
            .unwrap()
            .to_rgb8()
            .get_pixel(300, 300)
            .0
    }

    fn close(a: [u8; 3], b: [u8; 3]) -> bool {
        a.iter().zip(b).all(|(x, y)| x.abs_diff(y) < 8)
    }

    const RED: [u8; 3] = [200, 20, 20];
    const BLUE: [u8; 3] = [20, 20, 200];
    const GREEN: [u8; 3] = [20, 200, 20];

    #[test]
    fn kept_tracks_are_chapters_ending_where_the_next_starts() {
        let mut merged = track(60, "Gone", "Merged");
        merged.rejected = true;
        let tracks = vec![
            track(0, "A", "One"),
            merged,
            track(90, "", ""),
            track(200, "", "Titled Only"),
        ];
        let s = set(&tracks, &[]);
        let p = run(&s, tracks, None, true);
        let got: Vec<(u32, u32, &str)> = p
            .chapters
            .iter()
            .map(|c| (c.start_ms, c.end_ms, c.title.as_str()))
            .collect();
        assert_eq!(
            got,
            [
                (0, 90_000, "A - One"),
                (90_000, 200_000, "Unknown Track"),
                (200_000, 600_000, "Titled Only")
            ]
        );
        assert_eq!(p.episode.title.as_deref(), Some("September 28, 2026"));
        assert_eq!(p.episode.artist, None, "blank leaves the file's own");
        // No art anywhere: no pictures and no cover, never a stand-in card.
        assert_eq!(p.images(), 0);
        assert_eq!((p.episode.cover, p.cover_from), (None, None));
    }

    #[test]
    fn chapters_are_in_time_order_whatever_order_the_file_has() {
        // A hand-edited .md can list tracks out of order; the CHAPs still go in by start
        // (#33), and every chapter ends after it starts.
        let tracks = vec![
            track(120, "C", "x"),
            track(0, "A", "x"),
            track(60, "B", "x"),
        ];
        let s = set(&tracks, &[]);
        let p = run(&s, tracks, None, true);
        let got: Vec<(u32, u32)> = p.chapters.iter().map(|c| (c.start_ms, c.end_ms)).collect();
        assert_eq!(got, [(0, 60_000), (60_000, 120_000), (120_000, 600_000)]);
        assert_eq!(p.chapters[0].title, "A - x");
    }

    #[test]
    fn an_unidentified_star_doesnt_hide_an_identified_one() {
        let mut tracks = vec![
            track(0, "", ""),
            track(60, "Red", "x"),
            track(120, "Blue", "x"),
        ];
        tracks[0].is_episode_cover = true;
        tracks[2].is_episode_cover = true;
        let s = set(&tracks, &[("Red", RED), ("Blue", BLUE)]);
        assert_eq!(
            run(&s, tracks, None, true).cover_from,
            Some(CoverFrom::Track(2))
        );
    }

    #[test]
    fn the_cover_is_the_starred_tracks_else_the_first_with_art() {
        let tracks = vec![
            track(0, "Bare", "x"),
            track(60, "Red", "x"),
            track(120, "Blue", "x"),
        ];
        let s = set(&tracks, &[("Red", RED), ("Blue", BLUE)]);
        let p = run(&s, tracks.clone(), None, true);
        assert_eq!(p.cover_from, Some(CoverFrom::Track(1)));
        assert!(close(colour(p.episode.cover.as_ref().unwrap()), RED));
        assert_eq!(p.images(), 2);

        let mut starred = tracks.clone();
        starred[2].is_episode_cover = true;
        let p = run(&s, starred, None, true);
        assert_eq!(p.cover_from, Some(CoverFrom::Track(2)));
        assert!(close(colour(p.episode.cover.as_ref().unwrap()), BLUE));

        // A starred track with no art falls through to the first that has some.
        let mut starred = tracks;
        starred[0].is_episode_cover = true;
        assert_eq!(
            run(&s, starred, None, true).cover_from,
            Some(CoverFrom::Track(1))
        );
    }

    #[test]
    fn given_beats_saved_beats_starred_and_both_survive_no_artwork() {
        let mut tracks = vec![track(0, "Red", "x")];
        tracks[0].is_episode_cover = true;
        let s = set(&tracks, &[("Red", RED)]);
        fs::write(cover_path_for(&s.path), png(GREEN)).unwrap();

        let p = run(&s, tracks.clone(), None, true);
        assert_eq!(p.cover_from, Some(CoverFrom::SavedFile));
        assert!(close(colour(p.episode.cover.as_ref().unwrap()), GREEN));
        assert!(p.chapters[0].image.is_some(), "the track keeps its own art");

        let given = chapter_jpeg(&png(BLUE)).unwrap();
        let p = run(&s, tracks.clone(), Some(given.clone()), true);
        assert_eq!(
            (p.cover_from, p.episode.cover),
            (Some(CoverFrom::Given), Some(given))
        );

        let p = run(&s, tracks.clone(), None, false);
        assert_eq!(p.cover_from, Some(CoverFrom::SavedFile));
        assert_eq!(p.images(), 0);

        // Without either, --no-artwork embeds no cover at all.
        fs::remove_file(cover_path_for(&s.path)).unwrap();
        assert_eq!(run(&s, tracks, None, false).cover_from, None);
    }

    #[test]
    fn an_unreadable_saved_cover_is_reported_and_skipped() {
        let tracks = vec![track(0, "Red", "x")];
        let s = set(&tracks, &[("Red", RED)]);
        fs::write(cover_path_for(&s.path), "not an image").unwrap();
        let tl = Tracklist {
            source_file: "x.mp3".into(),
            tracks,
            generated_on: None,
            summary: None,
        };
        let mut unreadable = false;
        let p = prepare(
            &tl,
            &s.path,
            1000,
            &EpisodeTags::default(),
            None,
            true,
            &s.cache,
            &mut |n| unreadable |= matches!(n, Progress::SavedCoverUnreadable),
        );
        assert!(unreadable);
        assert_eq!(p.cover_from, Some(CoverFrom::Track(0)));
    }

    #[test]
    fn an_uploaded_cover_is_used_and_a_missing_one_said_so() {
        let mut tracks = vec![track(0, "A", "x"), track(60, "B", "y")];
        let name = "0123456789abcdef01234567";
        tracks[0].coverart_url = Some(format!("upload:{name}.jpg"));
        tracks[1].coverart_url = Some("upload:fedcba9876543210fedcba98.jpg".into());
        let s = set(&tracks[..0], &[]);
        let uploads = artwork_dir_for(&s.path);
        fs::create_dir(&uploads).unwrap();
        fs::write(uploads.join(format!("{name}.jpg")), png(GREEN)).unwrap();
        let tl = Tracklist {
            source_file: "x.mp3".into(),
            tracks,
            generated_on: None,
            summary: None,
        };
        let mut arts = Vec::new();
        let p = prepare(
            &tl,
            &s.path,
            120_000,
            &EpisodeTags::default(),
            None,
            true,
            &s.cache,
            &mut |n| {
                if let Progress::Looked(art) = n {
                    arts.push(art)
                }
            },
        );
        assert_eq!(arts, [Art::Found, Art::UploadMissing]);
        assert!(close(colour(p.chapters[0].image.as_ref().unwrap()), GREEN));
    }
}
