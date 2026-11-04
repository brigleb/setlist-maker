//! Cover art: where a track's cover comes from, the alternates the editor's picker offers, a
//! disk cache of both answers, and the picture a chapter embeds.
//!
//! - [`sources`]: Shazam's own URL, then iTunes, Deezer and MusicBrainz/Cover Art Archive,
//!   each reached through [`Fetch`], so tests never touch the network.
//! - [`cache`]: the bytes a lookup found (`<key>.src`), an empty marker when it found nothing
//!   (`<key>.fallback`), and the picker's candidates (`<key>.cands`), keyed exactly as Python
//!   keyed them, so a copied cache is a warm one.
//! - [`image`]: the chapter picture, which is the cover centre-cropped, resized and
//!   compressed, or nothing. Python's lower-third overlay and gradient card are gone.

pub mod cache;
pub mod fetch;
pub mod image;
pub mod sources;

use std::net::Ipv6Addr;
use std::sync::LazyLock;

use regex::Regex;
use serde::{Deserialize, Serialize};

pub use cache::ArtworkCache;
pub use fetch::{Fetch, Web};

/// Chapter artwork's edge, in pixels. Part of every cache key, as it was in Python.
pub const CHAPTER_IMAGE_SIZE: u32 = 600;

/// How many search results each source offers the picker. Small on purpose: MusicBrainz costs
/// one more request per candidate, and near-duplicates collapse anyway.
pub const CANDIDATES_PER_SOURCE: usize = 3;

/// One cover-art option, tagged with the source that offered it. `label` is the album or
/// release name when the API gives one, often the only thing telling two candidates apart.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArtworkCandidate {
    pub source: String,
    pub url: String,
    #[serde(default)]
    pub label: String,
}

impl ArtworkCandidate {
    pub fn new(source: &str, url: impl Into<String>, label: impl Into<String>) -> Self {
        ArtworkCandidate {
            source: source.into(),
            url: url.into(),
            label: label.into(),
        }
    }
}

/// True for a URL this module will download: http(s), with a host.
///
/// Artwork URLs aren't only ours: the picker lets a user paste one, which is saved to the
/// sidecar and fetched on every later run, so `file:`, `data:` and `ftp:` are refused (#20).
/// Python's `urlparse` rules: the scheme is case-insensitive, the host is what follows `//`
/// up to the path, and a bracketed host must be an IPv6 address (`http://[::1` is refused,
/// not an error).
pub fn is_fetchable_url(url: &str) -> bool {
    let Some((scheme, rest)) = url.split_once(':') else {
        return false;
    };
    if !matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https") {
        return false;
    }
    let Some(after) = rest.strip_prefix("//") else {
        return false;
    };
    let host = after.split(['/', '?', '#']).next().unwrap_or("");
    if host.is_empty() {
        return false;
    }
    match (host.find('['), host.find(']')) {
        (None, None) => true,
        (Some(open), Some(_)) => {
            let inside = host[open + 1..].split(']').next().unwrap_or("");
            inside.parse::<Ipv6Addr>().is_ok() || inside.starts_with('v')
        }
        _ => false,
    }
}

static CDN_SIZE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(\d+x\d+)(bb|cc)").unwrap());

/// A Shazam/Apple CDN cover URL at another size: `400x400cc` (or `bb`) becomes `600x600cc`.
pub fn resize_cover_art_url(url: &str, size: u32) -> String {
    CDN_SIZE
        .replace_all(url, format!("{size}x{size}$2").as_str())
        .into_owned()
}

static BRACKETED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s*[\(\[][^)\]]*[\)\]]").unwrap());
static FEATURING: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\s+(?:feat\.?|ft\.?|featuring)\s+.*").unwrap());

/// A search term without remix tags, featuring credits or bracketed noise:
/// `Track (Original Mix)` → `Track`, `Artist feat. Someone` → `Artist`.
pub fn clean_query(text: &str) -> String {
    let cleaned = BRACKETED.replace_all(text, "");
    FEATURING.replace_all(&cleaned, "").trim().to_string()
}

/// Repeats dropped by URL, keeping first-seen order (and so source priority).
pub fn dedupe(candidates: Vec<ArtworkCandidate>) -> Vec<ArtworkCandidate> {
    let mut seen = std::collections::HashSet::new();
    candidates
        .into_iter()
        .filter(|c| seen.insert(c.url.clone()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_http_with_a_host_is_fetchable() {
        for url in [
            "file:///etc/passwd",
            "data:image/png;base64,iVBORw0KGgo=",
            "ftp://example.com/cover.jpg",
            "javascript:alert(1)",
            "//example.com/cover.jpg",
            "not a url",
            "",
            "https:///a.jpg",
            "http://[::1",
            "https://[bad",
            "http://[",
            "https://][",
            "https://[bad]/x",
        ] {
            assert!(!is_fetchable_url(url), "{url:?}");
        }
        for url in [
            "https://example.com/a.jpg",
            "http://example.com/a.jpg",
            "HTTPS://example.com/a.jpg",
            "http://[::1]:8080/a.jpg",
            "https://example.com?x=1",
        ] {
            assert!(is_fetchable_url(url), "{url:?}");
        }
    }

    #[test]
    fn cdn_urls_resize_only_their_dimensions() {
        assert_eq!(
            resize_cover_art_url("https://is1-ssl.mzstatic.com/image/400x400cc.jpg", 600),
            "https://is1-ssl.mzstatic.com/image/600x600cc.jpg"
        );
        assert_eq!(
            resize_cover_art_url("https://example.com/art/100x100bb.jpg", 1200),
            "https://example.com/art/1200x1200bb.jpg"
        );
        let plain = "https://example.com/image.jpg";
        assert_eq!(resize_cover_art_url(plain, 600), plain);
        assert_eq!(
            resize_cover_art_url(
                "https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/12x34/400x400bb.jpg",
                600
            ),
            "https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/12x34/600x600bb.jpg"
        );
    }

    #[test]
    fn queries_lose_their_tags() {
        // Python's TestCleanQuery.
        assert_eq!(clean_query("Track Name (Original Mix)"), "Track Name");
        assert_eq!(clean_query("Title [Radio Edit]"), "Title");
        assert_eq!(clean_query("Artist feat. Someone"), "Artist");
        assert_eq!(clean_query("Artist ft. Someone"), "Artist");
        assert_eq!(clean_query("Artist Featuring Someone Else"), "Artist");
        assert_eq!(
            clean_query("Track (Extended Mix) [Remastered] feat. X"),
            "Track"
        );
        assert_eq!(clean_query("Clean Title"), "Clean Title");
        assert_eq!(clean_query(""), "");
    }

    #[test]
    fn dedupe_keeps_the_first_of_each_url() {
        let got = dedupe(vec![
            ArtworkCandidate::new("iTunes", "u1", "A"),
            ArtworkCandidate::new("Deezer", "u2", ""),
            ArtworkCandidate::new("Deezer", "u1", "B"),
        ]);
        assert_eq!(
            got,
            [
                ArtworkCandidate::new("iTunes", "u1", "A"),
                ArtworkCandidate::new("Deezer", "u2", "")
            ]
        );
    }
}
