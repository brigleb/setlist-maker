//! Where cover art is looked for: the track's own Shazam URL, then iTunes, Deezer and
//! MusicBrainz with Cover Art Archive. Python's `artwork.py`, request for request.
//!
//! Every source reads its whole answer inside one guard: a third-party API can answer 200 with
//! JSON of an unexpected shape (`null`, a list, an error object), and that is "no candidates",
//! never a panic in the middle of a four-hour run.

use std::sync::LazyLock;

use regex::Regex;
use reqwest::Url;
use serde_json::Value;

use super::{
    ArtworkCandidate, CANDIDATES_PER_SOURCE, Fetch, clean_query, dedupe, is_fetchable_url,
    resize_cover_art_url,
};

const ITUNES: &str = "https://itunes.apple.com/search";
const DEEZER: &str = "https://api.deezer.com/search";
const MUSICBRAINZ: &str = "https://musicbrainz.org/ws/2/recording";

fn with_query(base: &str, params: &[(&str, &str)]) -> String {
    Url::parse_with_params(base, params)
        .map(String::from)
        .unwrap_or_default()
}

fn json(fetch: &dyn Fetch, url: &str) -> Option<Value> {
    serde_json::from_slice(&fetch.get(url)?).ok()
}

/// iTunes' search, up to `limit` covers. One request whatever the limit.
pub fn itunes(
    fetch: &dyn Fetch,
    artist: &str,
    title: &str,
    size: u32,
    limit: usize,
) -> Vec<ArtworkCandidate> {
    let limit_s = limit.to_string();
    let url = with_query(
        ITUNES,
        &[
            ("term", &format!("{artist} {title}")),
            ("entity", "song"),
            ("limit", &limit_s),
        ],
    );
    let read = || -> Option<Vec<ArtworkCandidate>> {
        let data = json(fetch, &url)?;
        let results = rows(data.as_object()?.get("results"))?;
        let mut found = Vec::new();
        for result in results.iter().take(limit) {
            let result = result.as_object()?;
            let art = string(result.get("artworkUrl100"))?;
            if !art.is_empty() {
                found.push(ArtworkCandidate::new(
                    "iTunes",
                    art.replace("100x100bb", &format!("{size}x{size}bb")),
                    string(result.get("collectionName"))?,
                ));
            }
        }
        Some(found)
    };
    dedupe(read().unwrap_or_default())
}

/// A list field read as Python's `x.get(k) or []`: missing, null, false or empty is no rows;
/// anything else that isn't a list is a malformed answer.
fn rows(v: Option<&Value>) -> Option<Vec<Value>> {
    match v {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Some(Vec::new()),
        Some(Value::Array(a)) => Some(a.clone()),
        Some(Value::String(s)) if s.is_empty() => Some(Vec::new()),
        _ => None,
    }
}

/// A text field read as Python's `x.get(k) or ""`: a string, or nothing. Any other value is a
/// malformed answer.
fn string(v: Option<&Value>) -> Option<String> {
    match v {
        None | Some(Value::Null) => Some(String::new()),
        Some(Value::String(s)) => Some(s.clone()),
        _ => None,
    }
}

/// One Deezer search: its rows, `Some([])` for an answer with none, `None` when the request
/// failed. Retrying another query is only worth it when Deezer answered.
fn deezer_search(fetch: &dyn Fetch, query: &str) -> Option<Vec<Value>> {
    let data = json(fetch, &with_query(DEEZER, &[("q", query)]))?;
    // A malformed answer is a failed search, as Python's guard made it.
    rows(data.as_object()?.get("data"))
}

static DEEZER_SIZE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"/\d+x\d+-").unwrap());

/// Deezer's search, up to `limit` covers. Two queries at most: the advanced
/// `artist:"…" track:"…"` comes back empty for many real tracks (0 rows for Daft Punk / One
/// More Time, where the plain query finds 48), so a plain query follows an empty answer.
pub fn deezer(
    fetch: &dyn Fetch,
    artist: &str,
    title: &str,
    size: u32,
    limit: usize,
) -> Vec<ArtworkCandidate> {
    let mut found_rows = Vec::new();
    for query in [
        format!("artist:\"{artist}\" track:\"{title}\""),
        format!("{artist} {title}"),
    ] {
        let Some(result) = deezer_search(fetch, &query) else {
            break; // unreachable: a second query costs another timeout for nothing
        };
        if !result.is_empty() {
            found_rows = result;
            break;
        }
    }
    let read = || -> Option<Vec<ArtworkCandidate>> {
        let mut found = Vec::new();
        for row in found_rows.iter().take(limit) {
            let album = match row.as_object()?.get("album") {
                None | Some(Value::Null) => serde_json::Map::new(),
                Some(Value::Object(a)) => a.clone(),
                _ => return None,
            };
            let xl = string(album.get("cover_xl"))?;
            let art = if xl.is_empty() {
                string(album.get("cover_big"))?
            } else {
                xl
            };
            if !art.is_empty() {
                let resized = DEEZER_SIZE.replace_all(&art, format!("/{size}x{size}-").as_str());
                found.push(ArtworkCandidate::new(
                    "Deezer",
                    resized,
                    string(album.get("title"))?,
                ));
            }
        }
        Some(found)
    };
    dedupe(read().unwrap_or_default())
}

/// MusicBrainz' recording search, then each release's front cover from Cover Art Archive: the
/// one source whose cost grows with `limit`, since CAA says whether a release has a cover only
/// by being asked. Releases without one are skipped.
pub fn musicbrainz(
    fetch: &dyn Fetch,
    artist: &str,
    title: &str,
    limit: usize,
) -> Vec<ArtworkCandidate> {
    let limit_s = limit.to_string();
    let url = with_query(
        MUSICBRAINZ,
        &[
            (
                "query",
                &format!("artist:\"{artist}\" AND recording:\"{title}\""),
            ),
            ("fmt", "json"),
            ("limit", &limit_s),
        ],
    );
    let read = || -> Option<Vec<(String, String)>> {
        let data = json(fetch, &url)?;
        let mut releases: Vec<(String, String)> = Vec::new();
        for recording in rows(data.as_object()?.get("recordings"))? {
            for release in rows(recording.as_object()?.get("releases"))? {
                let release = release.as_object()?;
                let id = string(release.get("id"))?;
                if !id.is_empty() && !releases.iter().any(|(r, _)| *r == id) {
                    releases.push((id, string(release.get("title"))?));
                }
                if releases.len() >= limit {
                    break;
                }
            }
            if releases.len() >= limit {
                break;
            }
        }
        Some(releases)
    };
    let found = read()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|(id, label)| {
            let cover = format!("https://coverartarchive.org/release/{id}/front-500");
            let url = fetch.final_url(&cover)?;
            Some(ArtworkCandidate::new("Cover Art Archive", url, label))
        })
        .collect();
    dedupe(found)
}

/// Whether a cleaned-up query is worth a second iTunes search.
fn cleaned(artist: &str, title: &str) -> Option<(String, String)> {
    let (a, t) = (clean_query(artist), clean_query(title));
    (a != artist || t != title).then_some((a, t))
}

/// Every cover the search sources offer for one track, for the picker (#20): all of them, in
/// the waterfall's order, so the one in use normally sorts first. The track's saved URL is one
/// particular answer, not a source, so it isn't among them.
pub fn candidates(
    fetch: &dyn Fetch,
    artist: &str,
    title: &str,
    size: u32,
) -> Vec<ArtworkCandidate> {
    let n = CANDIDATES_PER_SOURCE;
    let mut found = itunes(fetch, artist, title, size, n);
    if let Some((a, t)) = cleaned(artist, title) {
        found.extend(itunes(fetch, &a, &t, size, n));
    }
    found.extend(deezer(fetch, artist, title, size, n));
    found.extend(musicbrainz(fetch, artist, title, n));
    dedupe(found)
}

/// A track's cover from the first source that yields one: its Shazam URL resized, then as
/// saved, iTunes, iTunes with a cleaned query (when that differs), Deezer, MusicBrainz. Each
/// search is asked for one result. `None` when nothing downloads.
pub fn fetch_artwork(
    fetch: &dyn Fetch,
    artist: &str,
    title: &str,
    coverart_url: Option<&str>,
    size: u32,
) -> Option<Vec<u8>> {
    let first = |v: Vec<ArtworkCandidate>| v.into_iter().next().map(|c| c.url);
    let download = |how: &str, url: Option<String>| -> Option<Vec<u8>> {
        let url = url.filter(|u| is_fetchable_url(u))?;
        match fetch.get(&url) {
            Some(data) if !data.is_empty() => {
                log::debug!("artwork for '{artist} - {title}' via {how}");
                Some(data)
            }
            _ => {
                log::debug!("artwork download failed via {how} for '{artist} - {title}'");
                None
            }
        }
    };
    let saved = coverart_url.filter(|u| !u.is_empty());
    let found = saved
        .and_then(|u| download("Shazam CDN (resized)", Some(resize_cover_art_url(u, size))))
        .or_else(|| saved.and_then(|u| download("Shazam CDN (original)", Some(u.into()))))
        .or_else(|| download("iTunes exact", first(itunes(fetch, artist, title, size, 1))))
        .or_else(|| {
            let (a, t) = cleaned(artist, title)?;
            download("iTunes cleaned", first(itunes(fetch, &a, &t, size, 1)))
        })
        .or_else(|| download("Deezer", first(deezer(fetch, artist, title, size, 1))))
        .or_else(|| download("MusicBrainz", first(musicbrainz(fetch, artist, title, 1))));
    if found.is_none() {
        log::debug!("no artwork for '{artist} - {title}'");
    }
    found
}

#[cfg(test)]
mod tests {
    use super::super::fetch::fake::Canned;
    use super::*;

    fn itunes_url(term: &str, limit: usize) -> String {
        with_query(
            ITUNES,
            &[
                ("term", term),
                ("entity", "song"),
                ("limit", &limit.to_string()),
            ],
        )
    }

    fn deezer_url(q: &str) -> String {
        with_query(DEEZER, &[("q", q)])
    }

    fn mb_url(artist: &str, title: &str, limit: usize) -> String {
        with_query(
            MUSICBRAINZ,
            &[
                (
                    "query",
                    &format!("artist:\"{artist}\" AND recording:\"{title}\""),
                ),
                ("fmt", "json"),
                ("limit", &limit.to_string()),
            ],
        )
    }

    #[test]
    fn queries_are_form_encoded_as_python_encodes_them() {
        assert_eq!(
            itunes_url("Altın Gün Kesik Çayır", 3),
            "https://itunes.apple.com/search?term=Alt%C4%B1n+G%C3%BCn+Kesik+%C3%87ay%C4%B1r&entity=song&limit=3"
        );
        assert_eq!(
            deezer_url("artist:\"Daft Punk\" track:\"One More Time\""),
            "https://api.deezer.com/search?q=artist%3A%22Daft+Punk%22+track%3A%22One+More+Time%22"
        );
    }

    #[test]
    fn itunes_offers_each_album_at_the_chapter_size() {
        let body = r#"{"results": [
            {"artworkUrl100": "https://x/100x100bb.jpg", "collectionName": "Single"},
            {"artworkUrl100": "https://y/100x100bb.jpg", "collectionName": null},
            {"artworkUrl100": "https://x/100x100bb.jpg", "collectionName": "Again"},
            {"artworkUrl100": ""}
        ]}"#;
        let f = Canned::default().body(&itunes_url("A T", 3), body);
        assert_eq!(
            itunes(&f, "A", "T", 600, 3),
            [
                ArtworkCandidate::new("iTunes", "https://x/600x600bb.jpg", "Single"),
                ArtworkCandidate::new("iTunes", "https://y/600x600bb.jpg", ""),
            ]
        );
        assert!(itunes(&Canned::default(), "A", "T", 600, 3).is_empty());
    }

    #[test]
    fn junk_answers_are_no_candidates() {
        // Python's TestSourceHelpersTolerateJunkResponses.
        for body in [
            "null",
            "[1, 2]",
            "\"a string\"",
            "42",
            r#"{"results": [1, 2]}"#,
            r#"{"results": null}"#,
        ] {
            let f = Canned::default().body(&itunes_url("A T", 3), body);
            assert!(itunes(&f, "A", "T", 600, 3).is_empty(), "{body}");
        }
        for body in [
            "null",
            "[1, 2]",
            r#"{"data": ["x"]}"#,
            r#"{"data": [{"album": 7}]}"#,
        ] {
            let f = Canned::default()
                .body(&deezer_url("artist:\"A\" track:\"T\""), body)
                .body(&deezer_url("A T"), body);
            assert!(deezer(&f, "A", "T", 600, 3).is_empty(), "{body}");
        }
        for body in [
            "null",
            "\"a string\"",
            r#"{"recordings": [{"releases": ["x"]}]}"#,
            r#"{"recordings": [7]}"#,
        ] {
            let f = Canned::default().body(&mb_url("A", "T", 3), body);
            assert!(musicbrainz(&f, "A", "T", 3).is_empty(), "{body}");
        }
    }

    #[test]
    fn deezer_retries_a_plain_query_only_after_an_empty_answer() {
        let advanced = deezer_url("artist:\"Daft Punk\" track:\"One More Time\"");
        let plain = deezer_url("Daft Punk One More Time");
        let rows = r#"{"data": [{"album": {"cover_xl": "https://d/1000x1000-000.jpg", "title": "Discovery"}},
                                {"album": {"cover_big": "https://d/500x500-000.jpg"}}]}"#;

        let f = Canned::default()
            .body(&advanced, r#"{"data": []}"#)
            .body(&plain, rows);
        // cover_xl, else cover_big, at the chapter size; the two rows are one URL then.
        assert_eq!(
            deezer(&f, "Daft Punk", "One More Time", 600, 3),
            [ArtworkCandidate::new(
                "Deezer",
                "https://d/600x600-000.jpg",
                "Discovery"
            )]
        );
        assert_eq!(f.asked(), [advanced.clone(), plain.clone()]);

        // The advanced query answered: no retry.
        let f = Canned::default().body(&advanced, rows);
        assert_eq!(deezer(&f, "Daft Punk", "One More Time", 600, 3).len(), 1);
        assert_eq!(f.asked(), std::slice::from_ref(&advanced));

        // The request failed: no retry either.
        let f = Canned::default();
        assert!(deezer(&f, "Daft Punk", "One More Time", 600, 3).is_empty());
        assert_eq!(f.asked(), [advanced]);
    }

    #[test]
    fn musicbrainz_asks_caa_per_release_and_skips_coverless_ones() {
        let body = r#"{"recordings": [
            {"releases": [{"id": "r1", "title": "Album"}, {"id": "r2", "title": "Comp"}]},
            {"releases": [{"id": "r1", "title": "Album"}, {"id": "r3"}]}
        ]}"#;
        let caa = |id: &str| format!("https://coverartarchive.org/release/{id}/front-500");
        let f = Canned::default()
            .body(&mb_url("A", "T", 3), body)
            .redirect(&caa("r1"), "https://archive.org/r1.jpg")
            .redirect(&caa("r3"), "https://archive.org/r3.jpg");
        assert_eq!(
            musicbrainz(&f, "A", "T", 3),
            [
                ArtworkCandidate::new("Cover Art Archive", "https://archive.org/r1.jpg", "Album"),
                ArtworkCandidate::new("Cover Art Archive", "https://archive.org/r3.jpg", ""),
            ]
        );
        assert_eq!(f.asked()[1..], [caa("r1"), caa("r2"), caa("r3")]);
    }

    #[test]
    fn candidates_ask_every_source_and_itunes_twice_when_cleaning_changes_the_query() {
        let f = Canned::default();
        candidates(&f, "A feat. B", "T (Remix)", 600);
        let asked = f.asked();
        assert_eq!(asked[0], itunes_url("A feat. B T (Remix)", 3));
        assert_eq!(asked[1], itunes_url("A T", 3));
        assert_eq!(
            asked[2],
            deezer_url("artist:\"A feat. B\" track:\"T (Remix)\"")
        );
        assert_eq!(asked[3], mb_url("A feat. B", "T (Remix)", 3));
        assert_eq!(asked.len(), 4, "Deezer unreachable: no retry");

        let f = Canned::default();
        candidates(&f, "A", "T", 600);
        assert_eq!(
            f.asked().iter().filter(|u| u.starts_with(ITUNES)).count(),
            1
        );
    }

    #[test]
    fn the_waterfall_stops_at_the_first_source_that_downloads() {
        // Shazam's URL resized first, then as saved.
        let f = Canned::default().body("https://s/600x600cc.jpg", "resized");
        assert_eq!(
            fetch_artwork(&f, "A", "T", Some("https://s/400x400cc.jpg"), 600).as_deref(),
            Some(&b"resized"[..])
        );
        let f = Canned::default().body("https://s/400x400cc.jpg", "original");
        assert_eq!(
            fetch_artwork(&f, "A", "T", Some("https://s/400x400cc.jpg"), 600).as_deref(),
            Some(&b"original"[..])
        );
        assert_eq!(
            f.asked(),
            ["https://s/600x600cc.jpg", "https://s/400x400cc.jpg"]
        );

        // Then the searches, each for one result, in order; an empty download moves on.
        let f = Canned::default()
            .body(
                &itunes_url("A T", 1),
                r#"{"results": [{"artworkUrl100": "https://i/100x100bb.jpg"}]}"#,
            )
            .body("https://i/600x600bb.jpg", "")
            .body(
                &deezer_url("artist:\"A\" track:\"T\""),
                r#"{"data": [{"album": {"cover_big": "https://d/500x500-0.jpg"}}]}"#,
            )
            .body("https://d/600x600-0.jpg", "deezer");
        assert_eq!(
            fetch_artwork(&f, "A", "T", None, 600).as_deref(),
            Some(&b"deezer"[..])
        );
        // A refused URL is never asked for, though the fake would answer it (#20).
        let f = Canned::default().body("file:///etc/passwd", "secret");
        assert_eq!(
            fetch_artwork(&f, "A", "T", Some("file:///etc/passwd"), 600),
            None
        );
        assert!(f.asked().iter().all(|u| u.starts_with("https://")));
    }

    #[test]
    fn nothing_anywhere_is_none() {
        let f = Canned::default();
        assert_eq!(fetch_artwork(&f, "A (x)", "T", None, 600), None);
        let asked = f.asked();
        assert_eq!(asked.len(), 4, "{asked:?}"); // iTunes, iTunes cleaned, Deezer, MusicBrainz
    }
}
