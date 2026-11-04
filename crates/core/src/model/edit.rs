//! `apply_track_edit`: the one place a correction lands on a track, for every front end.

use std::sync::LazyLock;

use regex::Regex;

use super::corrections::CorrectionsDb;
use super::track::Track;

// Reissue bookkeeping in a title: "(2009 Remaster)", "[Remastered]", "- 2012 Remaster". Mirrors
// `cleanTitle()` in `web_timeline.js`, whose Tidy removes exactly this.
const REISSUE: &str =
    r"(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:version|\d{4}))?";
static REISSUE_TAG: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?i)\s*[(\[]{REISSUE}[)\]]")).unwrap());
static REISSUE_DASH: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?i)\s+-\s+{REISSUE}\s*$")).unwrap());

/// A title without its remaster tag; "(Vocal)" and the like are left alone.
pub fn strip_reissue_tag(title: &str) -> String {
    let untagged = REISSUE_TAG.replace_all(title, "");
    REISSUE_DASH.replace_all(&untagged, "").trim().to_string()
}

/// Applies an artist/title correction to one track and records it. Returns whether the track
/// changed.
///
/// Both names are stripped first, so surrounding space is never what a change consists of
/// (#44). The first correction stamps `original_*` with what Shazam said, and later ones keep
/// it. It clears `coverart_url`, which is evidence about the *original* identification:
/// left in place, the chapter art would keep showing the misidentified track's cover (#30).
/// Two exceptions keep it: chosen artwork, which is the user's answer about this track (#20),
/// and tidying a remaster tag off the title, which names the same recording.
pub fn apply_track_edit(
    track: &mut Track,
    artist: &str,
    title: &str,
    corrections: Option<&mut CorrectionsDb>,
) -> bool {
    let artist = artist.trim();
    let title = title.trim();
    if artist == track.artist && title == track.title {
        return false;
    }
    if track.original_artist.is_none() {
        track.original_artist = Some(track.artist.clone());
    }
    if track.original_title.is_none() {
        track.original_title = Some(track.title.clone());
    }
    let same_recording =
        artist == track.artist && strip_reissue_tag(title) == strip_reissue_tag(&track.title);
    track.artist = artist.to_string();
    track.title = title.to_string();
    if !track.artwork_pinned && !same_recording {
        track.coverart_url = None;
    }
    if let Some(db) = corrections
        && track.was_corrected()
    {
        db.add_correction(
            track.original_artist.as_deref().unwrap_or(""),
            track.original_title.as_deref().unwrap_or(""),
            artist,
            title,
        );
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wrong() -> Track {
        Track {
            coverart_url: Some("https://cdn.shazam.com/wrong-album.jpg".into()),
            ..Track::new(0, "Wrong Artist", "Wrong Title")
        }
    }

    #[test]
    fn a_correction_clears_the_stale_coverart_url() {
        let mut t = wrong();
        assert!(apply_track_edit(&mut t, "Justice", "Genesis", None));
        assert_eq!(
            (t.artist.as_str(), t.title.as_str()),
            ("Justice", "Genesis")
        );
        assert_eq!(t.coverart_url, None);
    }

    #[test]
    fn tidying_a_remaster_tag_keeps_the_cover() {
        let url = "https://is1-ssl.mzstatic.com/right.jpg";
        for (tagged, clean) in [
            ("Computer Love (2009 Remaster)", "Computer Love"),
            ("Danger [Remastered]", "Danger"),
            ("Mirror - 2012 Remaster", "Mirror"),
        ] {
            let mut t = Track {
                coverart_url: Some(url.into()),
                ..Track::new(0, "Kraftwerk", tagged)
            };
            assert!(apply_track_edit(&mut t, "Kraftwerk", clean, None));
            assert_eq!(t.coverart_url.as_deref(), Some(url), "{tagged}");
        }
    }

    #[test]
    fn a_different_title_is_still_a_correction_of_the_art() {
        let mut t = Track {
            coverart_url: Some("https://is1-ssl.mzstatic.com/wrong.jpg".into()),
            ..Track::new(0, "Trio", "Da Da Da (Remastered)")
        };
        apply_track_edit(&mut t, "Trio", "Anna", None);
        assert_eq!(t.coverart_url, None);
    }

    #[test]
    fn strip_reissue_tag_leaves_other_parentheticals() {
        assert_eq!(
            strip_reissue_tag("Things Fall Apart (Vocal)"),
            "Things Fall Apart (Vocal)"
        );
        assert_eq!(
            strip_reissue_tag("Tress-Cun-Deo-La (Remastered 2025)"),
            "Tress-Cun-Deo-La"
        );
        assert_eq!(
            strip_reissue_tag("Song - Digitally Remastered Version"),
            "Song"
        );
    }

    #[test]
    fn an_unchanged_edit_keeps_the_art_and_reports_no_change() {
        let mut t = wrong();
        assert!(!apply_track_edit(
            &mut t,
            "Wrong Artist",
            "Wrong Title",
            None
        ));
        assert!(t.coverart_url.is_some());
        assert_eq!(t.original_artist, None);
    }

    #[test]
    fn records_the_correction_and_remembers_the_original() {
        let dir = tempfile::tempdir().unwrap();
        let mut db = CorrectionsDb::open(dir.path().join("corrections.json"));
        let mut t = wrong();
        apply_track_edit(&mut t, "Justice", "Genesis", Some(&mut db));
        assert_eq!(t.original_artist.as_deref(), Some("Wrong Artist"));
        assert_eq!(t.original_title.as_deref(), Some("Wrong Title"));
        assert_eq!(
            db.get_correction("Wrong Artist", "Wrong Title"),
            Some(("Justice".into(), "Genesis".into()))
        );
    }

    #[test]
    fn a_second_correction_keeps_the_first_original() {
        let mut t = wrong();
        apply_track_edit(&mut t, "Justice", "Genesis", None);
        apply_track_edit(&mut t, "Justice", "D.A.N.C.E.", None);
        assert_eq!(t.original_artist.as_deref(), Some("Wrong Artist"));
        assert_eq!(t.original_title.as_deref(), Some("Wrong Title"));
        assert_eq!(t.coverart_url, None);
    }

    #[test]
    fn fields_are_stored_stripped() {
        let mut t = wrong();
        assert!(apply_track_edit(&mut t, "  Justice  ", "  Genesis  ", None));
        assert_eq!(
            (t.artist.as_str(), t.title.as_str()),
            ("Justice", "Genesis")
        );
        assert!(apply_track_edit(&mut t, "   ", "Genesis", None));
        assert_eq!(t.artist, "");
    }

    #[test]
    fn stripping_does_not_invent_a_change() {
        let mut t = wrong();
        assert!(!apply_track_edit(
            &mut t,
            " Wrong Artist ",
            "Wrong Title ",
            None
        ));
        assert!(t.coverart_url.is_some());
    }

    #[test]
    fn a_correction_keeps_chosen_artwork() {
        let mut t = Track {
            coverart_url: Some("https://itunes/discovery.jpg".into()),
            artwork_pinned: true,
            ..Track::new(0, "Daft Punk", "Around the Wold")
        };
        assert!(apply_track_edit(
            &mut t,
            "Daft Punk",
            "Around the World",
            None
        ));
        assert_eq!(
            t.coverart_url.as_deref(),
            Some("https://itunes/discovery.jpg")
        );

        t.artwork_pinned = false;
        assert!(apply_track_edit(&mut t, "Justice", "Genesis", None));
        assert_eq!(t.coverart_url, None);
    }

    #[test]
    fn editing_back_to_the_original_records_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let mut db = CorrectionsDb::open(dir.path().join("corrections.json"));
        let mut t = wrong();
        apply_track_edit(&mut t, "Justice", "Genesis", None);
        apply_track_edit(&mut t, "Wrong Artist", "Wrong Title", Some(&mut db));
        assert!(db.corrections().is_empty());
    }
}
