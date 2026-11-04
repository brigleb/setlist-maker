//! Which Shazam answers name the same track.
//!
//! An (artist, title) pair is normalized (version, remix and featuring tags dropped, along
//! with brackets and punctuation) and clustered greedily: a key joins the first cluster whose
//! artist and title are each similar enough, else it starts one. So "One More Time" and "One
//! More Time (Radio Edit)" are one track, and the engine never sees a boundary between them.

use std::sync::LazyLock;

use regex::{Captures, Regex};
use serde_json::Value;

use super::difflib::ratio;

/// A normalized `(artist, title)`.
pub type Key = (String, String);

// Python's `\b` counts a character as part of a word when `str.isalnum()` says so (letters
// and numbers) or it's `_`; the regex crate's also counts marks and connectors. Every
// non-ASCII character ends up a space anyway, so it's first replaced by an ASCII stand-in,
// which leaves the patterns below ASCII-only and both regex engines in agreement:
// - `ı` (dotless i) and `ſ` (long s) by `I` and `S`. Python's IGNORECASE matches them as
//   `i` and `s`, so "Remıx" is a remix tag, while `[^a-z0-9]` still makes them a space
//   anywhere else ("Lıvely" is "l vely"). The text is lowercase, so nothing else is `I`/`S`.
// - another letter or number by `_`, a word character that no pattern matches;
// - anything else by `#`, which isn't a word character.
static NON_ASCII: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"([\p{L}\p{N}&&[^\x00-\x7F]])|[^\x00-\x7F]").unwrap());
static BRACKETED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[(\[{].*?[)\]}]").unwrap());
// IGNORECASE, as in Python, for the `I` and `S` above.
static FEAT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\b(feat\.?|ft\.?|featuring|with)\b.*").unwrap());
// Tokens that describe a *version* of a track rather than the track.
static VERSION_TOKENS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)\b(radio edit|original mix|extended mix|club mix|remaster(?:ed)?|remix|re-?edit|edit|version|vip|bootleg|mono|stereo|live|acoustic|instrumental)\b",
    )
    .unwrap()
});
static NON_ALNUM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());

/// Lowercased, with version and featuring tags, brackets and punctuation removed.
pub fn normalize_text(text: &str) -> String {
    let text = text.to_lowercase();
    let text = NON_ASCII.replace_all(&text, |c: &Captures| match &c[0] {
        "ı" => "I",
        "ſ" => "S",
        _ if c.get(1).is_some() => "_",
        _ => "#",
    });
    let text = BRACKETED.replace_all(&text, " ");
    let text = FEAT.replace_all(&text, " ");
    let text = VERSION_TOKENS.replace_all(&text, " ");
    let text = NON_ALNUM.replace_all(&text, " ");
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// The normalized key of a Shazam answer. A missing name is empty; so is one that isn't a
/// string, which Python would crash on and nothing writes.
pub fn normalized_key(info: &Value) -> Key {
    let field = |k: &str| normalize_text(info.get(k).and_then(Value::as_str).unwrap_or(""));
    (field("artist"), field("title"))
}

/// The first cluster `key` belongs to, if any.
pub fn find_cluster(
    key: &Key,
    clusters: &[Key],
    title_threshold: f64,
    artist_threshold: f64,
) -> Option<usize> {
    clusters.iter().position(|(artist, title)| {
        ratio(&key.0, artist) >= artist_threshold && ratio(&key.1, title) >= title_threshold
    })
}

/// The index of `key`'s cluster, starting a new one at the end when none matches.
pub fn assign_cluster(
    key: Key,
    clusters: &mut Vec<Key>,
    title_threshold: f64,
    artist_threshold: f64,
) -> usize {
    find_cluster(&key, clusters, title_threshold, artist_threshold).unwrap_or_else(|| {
        clusters.push(key);
        clusters.len() - 1
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn key(artist: &str, title: &str) -> Key {
        normalized_key(&json!({"artist": artist, "title": title}))
    }

    #[test]
    fn version_and_feature_tags_are_dropped() {
        assert_eq!(
            normalize_text("One More Time (Radio Edit)"),
            "one more time"
        );
        assert_eq!(
            normalize_text("Around the World - Radio Edit"),
            "around the world"
        );
        assert_eq!(normalize_text("Daft Punk feat. Pharrell"), "daft punk");
        assert_eq!(normalize_text("Song - 2009 Remastered"), "song 2009");
        assert_eq!(normalize_text("Re-Edit Me"), "me");
        assert_eq!(normalize_text("Editorial"), "editorial");
        assert_eq!(normalize_text("Sookie Sookie (Live)"), "sookie sookie");
        assert_eq!(normalize_text("  "), "");
    }

    #[test]
    fn non_ascii_becomes_a_separator_with_pythons_word_boundaries() {
        assert_eq!(normalize_text("Björk"), "bj rk");
        // İ lowercases to i + a combining dot, which isn't a word character in Python.
        assert_eq!(normalize_text("İstanbul"), "i stanbul");
        // A letter next to "live" keeps it from being a whole word...
        assert_eq!(normalize_text("éLive"), "live");
        // ...a mark doesn't (Python's \b, not the regex crate's).
        assert_eq!(normalize_text("\u{301}live"), "");
        // Python's IGNORECASE reads ı as i and ſ as s inside a tag, and nowhere else.
        assert_eq!(normalize_text("Remıx"), "");
        assert_eq!(normalize_text("Song wıth X"), "song");
        assert_eq!(normalize_text("ſtereo mix"), "mix");
        assert_eq!(normalize_text("Lıvely"), "l vely");
    }

    #[test]
    fn missing_or_non_string_names_are_empty() {
        assert_eq!(
            normalized_key(&json!({"title": "A"})),
            ("".into(), "a".into())
        );
        assert_eq!(normalized_key(&json!({"artist": 3, "title": "A"})).0, "");
    }

    #[test]
    fn fuzzy_merges_version_drift() {
        let mut clusters = Vec::new();
        let a = assign_cluster(
            key("Daft Punk", "Around the World"),
            &mut clusters,
            0.85,
            0.9,
        );
        let b = assign_cluster(
            key("Daft Punk", "Around the World - Radio Edit"),
            &mut clusters,
            0.85,
            0.9,
        );
        let c = assign_cluster(
            key("Daft Punk feat. Pharrell", "Around the World (Remix)"),
            &mut clusters,
            0.85,
            0.9,
        );
        assert_eq!((a, b, c), (0, 0, 0));
        assert_eq!(clusters.len(), 1);
    }

    #[test]
    fn different_songs_by_one_artist_stay_apart() {
        let mut clusters = Vec::new();
        assign_cluster(
            key("Daft Punk", "Around the World"),
            &mut clusters,
            0.85,
            0.9,
        );
        let other = assign_cluster(
            key("Daft Punk", "Harder Better Faster Stronger"),
            &mut clusters,
            0.85,
            0.9,
        );
        assert_eq!(other, 1);
    }

    #[test]
    fn near_names_merge_into_the_first_cluster() {
        // The oracle's trap: "artist 1" and "artist 11" score 0.94.
        let mut clusters = vec![key("Artist 1", "Track 1")];
        assert_eq!(
            find_cluster(&key("Artist 11", "Track 11"), &clusters, 0.85, 0.9),
            Some(0)
        );
        assert_eq!(assign_cluster(key("Zed", "Q"), &mut clusters, 0.85, 0.9), 1);
    }
}
