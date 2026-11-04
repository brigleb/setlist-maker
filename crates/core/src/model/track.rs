//! `Track` and `Tracklist`, and the sidecar rows they export.

use serde::Serialize;

/// One song in a tracklist: a start time plus an artist and a title, either of which may be
/// unknown.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Track {
    /// Seconds from the start of the set.
    pub timestamp: i64,
    pub artist: String,
    pub title: String,
    /// Merged into a neighbour in the editor; not written to either file.
    pub rejected: bool,
    pub shazam_url: Option<String>,
    pub album: Option<String>,
    pub coverart_url: Option<String>,
    /// Chosen artwork: the user picked this cover, so `coverart_url` is their answer about
    /// this track rather than Shazam's guess, and a later correction keeps it (#20).
    pub artwork_pinned: bool,
    /// Whose artwork becomes the episode cover; at most one track carries it.
    pub is_episode_cover: bool,
    /// Shazam match-confidence proxy, if known.
    pub confidence: Option<f64>,
    /// What Shazam said, kept once a correction changes it.
    pub original_artist: Option<String>,
    pub original_title: Option<String>,
}

impl Track {
    pub fn new(timestamp: i64, artist: impl Into<String>, title: impl Into<String>) -> Self {
        Track {
            timestamp,
            artist: artist.into(),
            title: title.into(),
            ..Track::default()
        }
    }

    /// `M:SS`, or `H:MM:SS` from an hour on.
    pub fn time_str(&self) -> String {
        // Python's floor division, so a negative time formats the way Python's did.
        let hours = self.timestamp.div_euclid(3600);
        let minutes = self.timestamp.rem_euclid(3600) / 60;
        let secs = self.timestamp.rem_euclid(60);
        if hours > 0 {
            format!("{hours}:{minutes:02}:{secs:02}")
        } else {
            format!("{minutes}:{secs:02}")
        }
    }

    /// Neither name is known. A field holding only spaces names nothing (#44).
    pub fn is_unidentified(&self) -> bool {
        self.artist.trim().is_empty() && self.title.trim().is_empty()
    }

    /// Changed by hand since Shazam named it.
    pub fn was_corrected(&self) -> bool {
        if self.original_artist.is_none() && self.original_title.is_none() {
            return false;
        }
        Some(&self.artist) != self.original_artist.as_ref()
            || Some(&self.title) != self.original_title.as_ref()
    }
}

/// The ordered list of tracks in a set, which alone decides which tracks exist.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Tracklist {
    /// The audio file's name, from the `# Tracklist:` header.
    pub source_file: String,
    pub tracks: Vec<Track>,
    /// `YYYY-MM-DD HH:MM`; a save stamps the current time when it's missing or empty.
    pub generated_on: Option<String>,
    /// The set description. The markdown is its only store: the sidecar doesn't carry it.
    pub summary: Option<String>,
}

/// One row of `<set>_tracklist.json`, fields in the order Python writes them.
#[derive(Clone, Debug, Serialize)]
pub struct SidecarRow<'a> {
    pub timestamp: i64,
    pub time: String,
    pub artist: &'a str,
    pub title: &'a str,
    pub rejected: bool,
    pub shazam_url: Option<&'a str>,
    pub album: Option<&'a str>,
    pub coverart_url: Option<&'a str>,
    pub artwork_pinned: bool,
    pub episode_cover: bool,
    pub confidence: Option<f64>,
}

impl Tracklist {
    /// The sidecar's rows: kept tracks only, in order.
    ///
    /// The sidecar stays a **bare list**, though the episode-cover choice belongs to the set:
    /// it is a flag on the one track whose art is used. Python's reader iterates the loaded
    /// JSON, and iterating an object yields its keys without raising, so wrapping the list
    /// would silently cost every track its `coverart_url`.
    pub fn sidecar_rows(&self) -> Vec<SidecarRow<'_>> {
        self.tracks
            .iter()
            .filter(|t| !t.rejected)
            .map(|t| SidecarRow {
                timestamp: t.timestamp,
                time: t.time_str(),
                artist: &t.artist,
                title: &t.title,
                rejected: t.rejected,
                shazam_url: t.shazam_url.as_deref(),
                album: t.album.as_deref(),
                coverart_url: t.coverart_url.as_deref(),
                artwork_pinned: t.artwork_pinned,
                episode_cover: t.is_episode_cover,
                confidence: t.confidence,
            })
            .collect()
    }

    /// The sidecar as Python's `json.dumps(..., indent=2)` lays it out, minus the `\uXXXX`
    /// escapes: non-ASCII is written as UTF-8, which parses to the same values.
    pub fn sidecar_json(&self) -> String {
        serde_json::to_string_pretty(&self.sidecar_rows()).expect("rows always serialize")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Tracklist {
        Tracklist {
            source_file: "test_mix.mp3".into(),
            generated_on: Some("2026-01-31 20:00".into()),
            tracks: vec![
                Track::new(0, "Daft Punk", "Around the World"),
                Track::new(180, "The Chemical Brothers", "Block Rockin' Beats"),
                Track::new(360, "", ""),
                Track::new(540, "Fatboy Slim", "Praise You"),
            ],
            summary: None,
        }
    }

    #[test]
    fn time_str_under_an_hour_hours_and_zero() {
        assert_eq!(Track::new(90, "A", "T").time_str(), "1:30");
        assert_eq!(Track::new(3661, "A", "T").time_str(), "1:01:01");
        assert_eq!(Track::new(0, "A", "T").time_str(), "0:00");
    }

    #[test]
    fn is_unidentified_needs_both_names_missing() {
        assert!(!Track::new(90, "Daft Punk", "Around the World").is_unidentified());
        assert!(Track::new(0, "", "").is_unidentified());
        assert!(!Track::new(0, "", "Some Title").is_unidentified());
        assert!(!Track::new(0, "Some Artist", "").is_unidentified());
    }

    #[test]
    fn a_whitespace_only_track_is_unidentified() {
        assert!(Track::new(0, "   ", "\t").is_unidentified());
        assert!(!Track::new(0, "   ", "Song").is_unidentified());
    }

    #[test]
    fn was_corrected() {
        assert!(!Track::new(90, "A", "T").was_corrected());
        let with = |a: &str, t: &str, oa: &str, ot: &str| Track {
            original_artist: Some(oa.into()),
            original_title: Some(ot.into()),
            ..Track::new(0, a, t)
        };
        assert!(!with("Artist", "Title", "Artist", "Title").was_corrected());
        assert!(with("Corrected", "Fixed", "Original", "Orig").was_corrected());
        assert!(with("Same Artist", "New Title", "Same Artist", "Old Title").was_corrected());
    }

    #[test]
    fn sidecar_rows() {
        let tl = sample();
        let rows = tl.sidecar_rows();
        assert_eq!(rows.len(), 4);
        assert_eq!(
            (rows[0].artist, rows[0].title, rows[0].timestamp),
            ("Daft Punk", "Around the World", 0)
        );
        assert_eq!(rows[0].time, "0:00");
        assert!(!rows[0].rejected);
    }

    #[test]
    fn sidecar_rows_exclude_rejected() {
        let mut tl = sample();
        tl.tracks[0].rejected = true;
        let rows = tl.sidecar_rows();
        assert_eq!(rows.len(), 3);
        assert!(rows.iter().all(|r| r.artist != "Daft Punk"));
    }

    #[test]
    fn an_empty_tracklist_is_an_empty_list() {
        assert_eq!(Tracklist::default().sidecar_json(), "[]");
    }

    #[test]
    fn the_sidecar_carries_the_curation_fields_in_pythons_order() {
        let tl = Tracklist {
            tracks: vec![Track {
                coverart_url: Some("https://x/a.jpg".into()),
                artwork_pinned: true,
                is_episode_cover: true,
                confidence: Some(0.95),
                ..Track::new(0, "A", "T")
            }],
            ..Tracklist::default()
        };
        assert_eq!(
            tl.sidecar_json(),
            r#"[
  {
    "timestamp": 0,
    "time": "0:00",
    "artist": "A",
    "title": "T",
    "rejected": false,
    "shazam_url": null,
    "album": null,
    "coverart_url": "https://x/a.jpg",
    "artwork_pinned": true,
    "episode_cover": true,
    "confidence": 0.95
  }
]"#
        );
    }

    #[test]
    fn untouched_tracks_export_the_curation_fields_as_false() {
        let tl = sample();
        let v: serde_json::Value = serde_json::from_str(&tl.sidecar_json()).unwrap();
        for row in v.as_array().unwrap() {
            assert_eq!(row["artwork_pinned"], false);
            assert_eq!(row["episode_cover"], false);
        }
    }
}
