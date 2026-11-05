//! The JSON the page reads and the edits it sends back: Python's `tracklist_to_api`,
//! `probes_to_api` and `apply_edits`, with no I/O.

use serde_json::{Map, Value, json};

use hindsight_core::artwork::is_fetchable_url;
use hindsight_core::model::uploads::is_upload_ref;
use hindsight_core::model::{CorrectionsDb, Probe, Track, Tracklist, apply_track_edit};

/// A payload the page shouldn't have sent: a 400, and nothing applied.
#[derive(Debug, PartialEq)]
pub struct BadEdit(pub String);

impl std::fmt::Display for BadEdit {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// The tracklist as the page reads it. `index` is the track's position, which the save
/// payload sends back so an edit lands on the right track.
pub fn tracklist_to_api(tracklist: &Tracklist) -> Value {
    let tracks: Vec<Value> = tracklist
        .tracks
        .iter()
        .enumerate()
        .map(|(i, t)| {
            json!({
                "index": i,
                "timestamp": t.timestamp,
                "time": t.time_str(),
                "artist": t.artist,
                "title": t.title,
                "rejected": t.rejected,
                "is_unidentified": t.is_unidentified(),
                "coverart_url": t.coverart_url,
                "episode_cover": t.is_episode_cover,
                "original_artist": t.original_artist,
                "original_title": t.original_title,
            })
        })
        .collect();
    json!({
        "source_file": tracklist.source_file,
        "summary": tracklist.summary,
        "tracks": tracks,
    })
}

/// One probe as a pin: where it listened, why, and what came back.
pub fn probe_to_api(p: &Probe) -> Value {
    let field = |k: &str| {
        p.result
            .as_ref()
            .and_then(|r| r.get(k))
            .cloned()
            .unwrap_or(Value::Null)
    };
    json!({
        "t": p.t,
        "window": p.window,
        "purpose": p.purpose,
        "artist": field("artist"),
        "title": field("title"),
        "confidence": field("confidence"),
        "coverart_url": field("coverart_url"),
    })
}

/// A run's probes in the order it asked for them, which is what a replay shows.
pub fn probes_to_api(probes: &[Probe], duration: Option<f64>) -> Value {
    json!({
        "duration": duration,
        "probes": probes.iter().map(probe_to_api).collect::<Vec<_>>(),
    })
}

/// Python's truthiness, for `bool(edit.get(...))`.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// A text field, stripped; absent or null is blank.
fn text(edit: &Map<String, Value>, key: &str) -> Result<String, BadEdit> {
    match edit.get(key) {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.trim().to_string()),
        Some(other) => Err(BadEdit(format!("{key} must be text -- got {other}"))),
    }
}

/// The artwork URL an edit pins, validated; `None` is "back to automatic". Only http(s) or an
/// `upload:` reference: this URL is saved to the sidecar and fetched by every later run.
fn picked_artwork_url(edit: &Map<String, Value>) -> Result<Option<String>, BadEdit> {
    let url = text(edit, "coverart_url")?;
    if url.is_empty() {
        return Ok(None);
    }
    if !(is_fetchable_url(&url) || is_upload_ref(Some(&url))) {
        return Err(BadEdit(format!(
            "artwork URL must be http:// or https:// -- got {url:?}"
        )));
    }
    Ok(Some(url))
}

/// The start an indexed edit moves its track to, or `None` to leave it: whole seconds >= 0
/// (rounded half to even, as Python's `round`), and never a bool, NaN or infinity.
fn moved_start(edit: &Map<String, Value>, track: &Track) -> Option<i64> {
    let value = edit.get("timestamp")?.as_f64()?;
    if !value.is_finite() || value < 0.0 {
        return None;
    }
    let seconds = value.round_ties_even() as i64;
    (seconds != track.timestamp).then_some(seconds)
}

/// An inserted track's start: Python's `max(0, int(x or 0))`, where anything unreadable is 0.
fn inserted_start(edit: &Map<String, Value>) -> i64 {
    let value = match edit.get("timestamp") {
        Some(Value::Number(n)) => n
            .as_f64()
            .filter(|f| f.is_finite())
            .map(|f| f.trunc() as i64),
        Some(Value::String(s)) => s.trim().parse::<i64>().ok(),
        Some(Value::Bool(b)) => Some(i64::from(*b)),
        _ => None,
    };
    value.unwrap_or(0).max(0)
}

/// One paragraph, the house style (#16); blank is no description.
pub fn normalize_summary(value: &Value) -> Result<Option<String>, BadEdit> {
    let text = match value {
        Value::Null => return Ok(None),
        Value::String(s) => s.split_whitespace().collect::<Vec<_>>().join(" "),
        other => return Err(BadEdit(format!("summary must be text -- got {other}"))),
    };
    Ok((!text.is_empty()).then_some(text))
}

/// Applies the page's edits in place, recording corrections. Existing tracks are found by
/// their `index`; an edit with none is a track inserted in the page, appended and sorted into
/// place (an insert isn't a correction, so none is learned). A moved start re-sorts too, so
/// the caller's order check sees it. `summary`, when sent, replaces the description.
///
/// Two artwork keys are only present when the user made that choice. `coverart_url` pins a
/// cover, applied *after* `apply_track_edit` (which clears the URL on a correction), so a
/// pick and a typo fix in one save keep the pick. `episode_cover` is exclusive, the last track
/// a payload marks wins, and is refused on a rejected track, which the sidecar doesn't carry.
///
/// Everything is checked before anything changes, so a bad row can't half-apply a save.
/// `Ok(true)` when an index now names another track (a row was inserted or a moved start
/// re-sorted the list), which is what makes the page's indices stale.
pub fn apply_edits(
    tracklist: &mut Tracklist,
    edits: &[Map<String, Value>],
    mut corrections: Option<&mut CorrectionsDb>,
    summary: Option<&Value>,
) -> Result<bool, BadEdit> {
    for edit in edits {
        text(edit, "artist")?;
        text(edit, "title")?;
        if edit.contains_key("coverart_url") {
            picked_artwork_url(edit)?;
        }
    }
    let summary = summary.map(normalize_summary).transpose()?;

    let count = tracklist.tracks.len();
    let mut inserted = Vec::new();
    let mut moved = false;
    let mut chosen_cover = None;
    for edit in edits {
        let artist = text(edit, "artist")?;
        let title = text(edit, "title")?;
        let index = match edit.get("index") {
            None | Some(Value::Null) => {
                inserted.push(Track {
                    rejected: edit.get("rejected").is_some_and(truthy),
                    ..Track::new(inserted_start(edit), artist, title)
                });
                continue;
            }
            Some(v) => v.as_u64().map(|i| i as usize),
        };
        let Some(i) = index.filter(|i| *i < count) else {
            continue;
        };
        let track = &mut tracklist.tracks[i];
        if let Some(to) = moved_start(edit, track) {
            track.timestamp = to;
            moved = true;
        }
        apply_track_edit(track, &artist, &title, corrections.as_deref_mut());
        if let Some(r) = edit.get("rejected") {
            track.rejected = truthy(r);
        }
        if edit.contains_key("coverart_url") {
            track.coverart_url = picked_artwork_url(edit)?;
            track.artwork_pinned = track.coverart_url.is_some();
        }
        if let Some(star) = edit.get("episode_cover") {
            let starred = truthy(star) && !track.rejected;
            track.is_episode_cover = starred;
            if starred {
                chosen_cover = Some(i);
            }
        }
    }
    if let Some(chosen) = chosen_cover {
        for (i, track) in tracklist.tracks.iter_mut().enumerate() {
            if i != chosen {
                track.is_episode_cover = false;
            }
        }
    }
    let mut reordered = !inserted.is_empty();
    if reordered || moved {
        tracklist.tracks.extend(inserted);
        // Stable: ties keep their order. Sorted by position so the move can be checked.
        let mut order: Vec<usize> = (0..tracklist.tracks.len()).collect();
        order.sort_by_key(|&i| tracklist.tracks[i].timestamp);
        reordered |= order.iter().enumerate().any(|(at, &i)| at != i);
        let mut tracks: Vec<Option<Track>> = tracklist.tracks.drain(..).map(Some).collect();
        tracklist.tracks = order
            .iter()
            .map(|&i| tracks[i].take().expect("each once"))
            .collect();
    }
    if let Some(summary) = summary {
        tracklist.summary = summary;
    }
    Ok(reordered)
}

#[cfg(test)]
mod tests {
    use super::*;
    use hindsight_core::model::parse_markdown_tracklist;

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

    fn edits(v: Value) -> Vec<Map<String, Value>> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|e| e.as_object().unwrap().clone())
            .collect()
    }

    fn apply(tl: &mut Tracklist, v: Value) {
        apply_edits(tl, &edits(v), None, None).unwrap();
    }

    fn times(tl: &Tracklist) -> Vec<i64> {
        tl.tracks.iter().map(|t| t.timestamp).collect()
    }

    #[test]
    fn the_api_shape() {
        let mut tl = sample();
        tl.tracks[1].is_episode_cover = true;
        let api = tracklist_to_api(&tl);
        assert_eq!(api["source_file"], "test_mix.mp3");
        assert!(api.as_object().unwrap().contains_key("summary"));
        let first = &api["tracks"][0];
        assert_eq!(
            (first["index"].clone(), first["artist"].clone()),
            (json!(0), json!("Daft Punk"))
        );
        assert_eq!(first["time"], "0:00");
        assert_eq!(first["is_unidentified"], false);
        assert_eq!(api["tracks"][2]["is_unidentified"], true);
        assert_eq!(api["tracks"][1]["episode_cover"], true);
        assert_eq!(api["tracks"][0]["episode_cover"], false);
    }

    #[test]
    fn an_edit_lands_by_index_and_records_the_correction() {
        let dir = tempfile::tempdir().unwrap();
        let mut db = CorrectionsDb::open(dir.path().join("corrections.json"));
        let mut tl = sample();
        let e = edits(json!([
            {"index": 0, "artist": "Daft Punk", "title": "Harder Better", "rejected": false},
            {"index": 1, "artist": "The Chemical Brothers", "title": "Block Rockin' Beats", "rejected": true},
            {"index": 99, "artist": "X", "title": "Y"},
        ]));
        apply_edits(&mut tl, &e, Some(&mut db), None).unwrap();
        assert_eq!(tl.tracks[0].title, "Harder Better");
        assert!(tl.tracks[0].was_corrected());
        assert_eq!(
            tl.tracks[0].original_title.as_deref(),
            Some("Around the World")
        );
        assert!(tl.tracks[1].rejected);
        assert_eq!(
            db.get_correction("Daft Punk", "Around the World"),
            Some(("Daft Punk".into(), "Harder Better".into()))
        );
    }

    #[test]
    fn a_correction_drops_stale_art_and_a_rejection_keeps_it() {
        let mut tl = sample();
        tl.tracks[0].coverart_url = Some("https://cdn/wrong.jpg".into());
        tl.tracks[1].coverart_url = Some("https://cdn/right.jpg".into());
        apply(
            &mut tl,
            json!([
                {"index": 0, "artist": "Justice", "title": "Genesis"},
                {"index": 1, "artist": "The Chemical Brothers", "title": "Block Rockin' Beats", "rejected": true},
            ]),
        );
        assert_eq!(tl.tracks[0].coverart_url, None);
        assert!(!tl.tracks[0].artwork_pinned);
        assert_eq!(
            tl.tracks[1].coverart_url.as_deref(),
            Some("https://cdn/right.jpg")
        );
    }

    #[test]
    fn inserts_are_sorted_into_place_and_learn_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let mut db = CorrectionsDb::open(dir.path().join("corrections.json"));
        let mut tl = sample();
        let e = edits(json!([
            {"index": 0, "artist": "Daft Punk", "title": "One More Time", "rejected": false},
            {"artist": "Aphex Twin", "title": "Windowlicker", "timestamp": 90},
            {"index": 3, "artist": "Fatboy Slim", "title": "Praise You", "rejected": true},
            {"artist": "Bonobo", "title": "Kerala", "timestamp": 600},
        ]));
        apply_edits(&mut tl, &e, Some(&mut db), None).unwrap();
        assert_eq!(times(&tl), [0, 90, 180, 360, 540, 600]);
        assert_eq!(tl.tracks[0].title, "One More Time");
        assert_eq!(tl.tracks[1].artist, "Aphex Twin");
        assert!(tl.tracks[4].rejected);
        assert!(!tl.tracks[1].was_corrected());
        assert_eq!(db.get_correction("Aphex Twin", "Windowlicker"), None);
    }

    #[test]
    fn an_insert_with_no_start_goes_after_the_tracks_already_at_zero() {
        let mut tl = sample();
        apply(
            &mut tl,
            json!([{"artist": "A", "title": "B"}, {"artist": "C", "timestamp": "x"}]),
        );
        assert_eq!(tl.tracks.len(), 6);
        assert_eq!(
            tl.tracks[..3]
                .iter()
                .map(|t| t.artist.as_str())
                .collect::<Vec<_>>(),
            ["Daft Punk", "A", "C"]
        );
    }

    #[test]
    fn the_summary_is_set_cleared_or_left() {
        let mut tl = sample();
        let set = |tl: &mut Tracklist, v: Option<Value>| {
            apply_edits(tl, &[], None, v.as_ref()).unwrap();
        };
        set(&mut tl, Some(json!("Line one.\n\nLine two.   Extra")));
        assert_eq!(tl.summary.as_deref(), Some("Line one. Line two. Extra"));
        set(&mut tl, None);
        assert_eq!(tl.summary.as_deref(), Some("Line one. Line two. Extra"));
        set(&mut tl, Some(json!("   ")));
        assert_eq!(tl.summary, None);
        tl.summary = Some("x".into());
        set(&mut tl, Some(Value::Null));
        assert_eq!(tl.summary, None);
        assert!(apply_edits(&mut tl, &[], None, Some(&json!(3))).is_err());
    }

    #[test]
    fn a_summary_and_an_insert_round_trip_through_the_markdown() {
        let mut tl = sample();
        let e = edits(json!([{"artist": "Caribou", "title": "Odessa", "timestamp": 270}]));
        apply_edits(&mut tl, &e, None, Some(&json!("A sweaty warehouse set."))).unwrap();
        let back = parse_markdown_tracklist(&tl.to_markdown()).tracklist;
        assert_eq!(back.summary.as_deref(), Some("A sweaty warehouse set."));
        assert_eq!(times(&back), [0, 180, 270, 360, 540]);
        assert_eq!(
            (
                back.tracks[2].artist.as_str(),
                back.tracks[2].title.as_str()
            ),
            ("Caribou", "Odessa")
        );
    }

    #[test]
    fn a_picked_cover_survives_the_correction_made_with_it() {
        let mut tl = sample();
        tl.tracks[0].coverart_url = Some("https://cdn/wrong.jpg".into());
        apply(
            &mut tl,
            json!([{"index": 0, "artist": "Daft Punk", "title": "Around the World (Radio Edit)",
                    "coverart_url": "https://itunes/discovery.jpg"}]),
        );
        assert_eq!(
            tl.tracks[0].coverart_url.as_deref(),
            Some("https://itunes/discovery.jpg")
        );
        assert!(tl.tracks[0].artwork_pinned);
        // "Automatic" sends null: back to the waterfall.
        apply(
            &mut tl,
            json!([{"index": 0, "artist": "Daft Punk", "title": "Around the World (Radio Edit)", "coverart_url": null}]),
        );
        assert_eq!(tl.tracks[0].coverart_url, None);
        assert!(!tl.tracks[0].artwork_pinned);
    }

    #[test]
    fn a_non_http_cover_is_refused_before_anything_changes() {
        for url in [
            "file:///etc/passwd",
            "data:image/png;base64,AAAA",
            "ftp://x/a.jpg",
            "nonsense",
        ] {
            let mut tl = sample();
            let e = edits(json!([
                {"index": 0, "artist": "Changed", "title": "Changed"},
                {"index": 1, "artist": "x", "title": "y", "coverart_url": url},
            ]));
            let err = apply_edits(&mut tl, &e, None, None).unwrap_err();
            assert!(err.0.contains("http"), "{err}");
            assert_eq!(tl, sample(), "{url}: nothing may be applied");
        }
        let mut tl = sample();
        apply(
            &mut tl,
            json!([{"index": 1, "artist": "x", "title": "y", "coverart_url": "upload:0123456789abcdef01234567.jpg"}]),
        );
        assert!(tl.tracks[1].artwork_pinned);
    }

    #[test]
    fn the_episode_cover_is_exclusive_and_never_on_a_rejected_track() {
        let edit = |i: usize, tl: &Tracklist, extra: Value| {
            let mut e =
                json!({"index": i, "artist": tl.tracks[i].artist, "title": tl.tracks[i].title});
            e.as_object_mut()
                .unwrap()
                .extend(extra.as_object().unwrap().clone());
            e
        };
        let mut tl = sample();
        tl.tracks[0].is_episode_cover = true;
        let e = json!([
            edit(0, &tl, json!({"episode_cover": true})),
            edit(1, &tl, json!({"episode_cover": true}))
        ]);
        apply(&mut tl, e);
        let stars = |tl: &Tracklist| {
            tl.tracks
                .iter()
                .map(|t| t.is_episode_cover)
                .collect::<Vec<_>>()
        };
        assert_eq!(stars(&tl), [false, true, false, false]);
        // No key: the choice stands.
        let e = json!([edit(0, &tl, json!({}))]);
        apply(&mut tl, e);
        assert_eq!(stars(&tl), [false, true, false, false]);
        // A rejected track can't take it, and doesn't clear it.
        let e = json!([edit(
            2,
            &tl,
            json!({"rejected": true, "episode_cover": true})
        )]);
        apply(&mut tl, e);
        assert_eq!(stars(&tl), [false, true, false, false]);
        // Rejecting the starred track clears its star.
        let e = json!([edit(
            1,
            &tl,
            json!({"rejected": true, "episode_cover": true})
        )]);
        apply(&mut tl, e);
        assert_eq!(stars(&tl), [false, false, false, false]);
    }

    #[test]
    fn only_a_change_of_order_reports_reordered() {
        let mut tl = sample();
        let re = |tl: &mut Tracklist, v: Value| apply_edits(tl, &edits(v), None, None).unwrap();
        assert!(!re(
            &mut tl,
            json!([{"index": 0, "artist": "X", "title": "Y"}])
        ));
        // Moved, but still between its neighbours.
        assert!(!re(
            &mut tl,
            json!([{"index": 1, "artist": "The Chemical Brothers", "title": "Block Rockin' Beats", "timestamp": 200}])
        ));
        assert!(re(
            &mut tl,
            json!([{"index": 3, "artist": "Fatboy Slim", "title": "Praise You", "timestamp": 100}])
        ));
        assert!(re(
            &mut tl,
            json!([{"artist": "New", "title": "Row", "timestamp": 999}])
        ));
    }

    #[test]
    fn a_moved_start_resorts_and_a_bad_one_is_ignored() {
        let mut tl = sample();
        apply(
            &mut tl,
            json!([{"index": 3, "artist": "Fatboy Slim", "title": "Praise You", "timestamp": 300.4}]),
        );
        assert_eq!(times(&tl), [0, 180, 300, 360]);
        assert_eq!(tl.tracks[2].title, "Praise You");
        assert!(!tl.tracks[2].was_corrected());
        for bad in [json!(null), json!("1:23"), json!(-5), json!(true)] {
            let mut tl = sample();
            apply(
                &mut tl,
                json!([{"index": 1, "artist": "The Chemical Brothers", "title": "Block Rockin' Beats", "timestamp": bad}]),
            );
            assert_eq!(times(&tl), [0, 180, 360, 540], "{bad}");
        }
    }

    #[test]
    fn probes_keep_the_order_they_were_asked_in() {
        let probes = [
            Probe {
                t: 450.0,
                window: 30.0,
                purpose: "coverage".into(),
                result: Some(
                    json!({"artist": "A", "title": "a", "confidence": 0.66, "coverart_url": "https://x/a.jpg"}),
                ),
                offsets: None,
            },
            Probe {
                t: 100.0,
                window: 12.0,
                purpose: "refine".into(),
                result: None,
                offsets: None,
            },
        ];
        let api = probes_to_api(&probes, Some(900.0));
        assert_eq!(api["duration"], 900.0);
        assert_eq!(
            api["probes"][0],
            json!({"t": 450.0, "window": 30.0, "purpose": "coverage", "artist": "A",
                   "title": "a", "confidence": 0.66, "coverart_url": "https://x/a.jpg"})
        );
        assert_eq!(api["probes"][1]["t"], 100.0);
        assert_eq!(api["probes"][1]["artist"], Value::Null);
    }
}
