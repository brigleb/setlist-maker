//! One request to Shazam's tag endpoint, and its answer in the shape a progress file stores.
//!
//! The request copies shazamio 0.8.1's `send_recognize_request_v2` (URL, headers, body),
//! except for one fixed User-Agent where shazamio picks one at random.
//!
//! Owning the client is what lets a run tell "Shazam doesn't know this" from "Shazam didn't
//! answer". shazamio retried a 429 twenty times inside one call and then handed back an
//! exception carrying no status, which Python reported as an unidentified sample. Here only a
//! 2xx JSON answer decides identified or not; everything else is a [`NoAnswer`], which the run
//! retries and never stores.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value, json};

use super::Fingerprint;
use crate::engine::py;

const URL: &str = "https://amp.shazam.com/discovery/v5/en-US/GB/{device}/-/tag/{u1}/{u2}\
?sync=true&webv3=true&sampling=true&connected=&shazamapiversion=v3&sharehub=true\
&hubv5minorversion=v5.1&hidelb=true&video=v3";
const USER_AGENT: &str = "Dalvik/2.1.0 (Linux; U; Android 5.0.2; VS980 4G Build/LRX22G)";
const TIMEOUT: Duration = Duration::from_secs(30);

/// What a sample heard, as the progress file stores it: the answer (`None` when Shazam
/// knows no match) and where in the matched song each match aligned.
#[derive(Clone, Debug, PartialEq)]
pub struct Heard {
    pub result: Option<Value>,
    pub offsets: Option<Vec<Value>>,
}

/// Shazam gave no answer: not "unidentified", just nothing to go on.
#[derive(Clone, Debug, PartialEq)]
pub enum NoAnswer {
    /// HTTP 429: rate limited.
    Throttled,
    /// Anything else: a network failure, another status, a body that isn't JSON.
    Failed(String),
}

impl std::fmt::Display for NoAnswer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            NoAnswer::Throttled => write!(f, "Shazam is throttling (HTTP 429)"),
            NoAnswer::Failed(why) => write!(f, "no answer from Shazam: {why}"),
        }
    }
}

pub struct Client {
    http: reqwest::blocking::Client,
}

impl Client {
    pub fn new() -> Result<Client, NoAnswer> {
        let http = reqwest::blocking::Client::builder()
            .timeout(TIMEOUT)
            .build()
            .map_err(|e| NoAnswer::Failed(e.to_string()))?;
        Ok(Client { http })
    }

    pub fn recognize(&self, fp: &Fingerprint) -> Result<Heard, NoAnswer> {
        let u1 = uuid::Uuid::new_v4();
        let device = ["iphone", "android", "web"][(u1.as_bytes()[0] % 3) as usize];
        let url = URL
            .replace("{device}", device)
            .replace("{u1}", &u1.to_string().to_uppercase())
            .replace("{u2}", &uuid::Uuid::new_v4().to_string().to_uppercase());
        let now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u32)
            .unwrap_or(0);
        let body = json!({
            "timezone": "Europe/Moscow",
            "signature": {"uri": fp.uri, "samplems": fp.excerpt_ms},
            "timestamp": now_ms,
            "context": {},
            "geolocation": {},
        });
        let resp = self
            .http
            .post(url)
            .header("X-Shazam-Platform", "IPHONE")
            .header("X-Shazam-AppVersion", "14.1.0")
            .header("Accept", "*/*")
            .header("Accept-Language", "en-US")
            .header("Accept-Encoding", "gzip, deflate")
            .header("User-Agent", USER_AGENT)
            .header("Content-Type", "application/json")
            .body(body.to_string())
            .send()
            .map_err(|e| NoAnswer::Failed(e.to_string()))?;
        let status = resp.status().as_u16();
        let bytes = resp.bytes().map_err(|e| NoAnswer::Failed(e.to_string()))?;
        parse_answer(status, &bytes)
    }
}

/// Shazam's response, read the way Python's `identify_sample_with_retry` read it.
pub fn parse_answer(status: u16, body: &[u8]) -> Result<Heard, NoAnswer> {
    if status == 429 {
        return Err(NoAnswer::Throttled);
    }
    if !(200..300).contains(&status) {
        return Err(NoAnswer::Failed(format!("HTTP {status}")));
    }
    let response: Value = serde_json::from_slice(body).map_err(|e| {
        let text = String::from_utf8_lossy(body);
        NoAnswer::Failed(format!("HTTP {status}, not JSON ({e}): {text:.120}"))
    })?;
    // Python's `if result and "track" in result`.
    let Some(track) = response.get("track") else {
        return Ok(Heard {
            result: None,
            offsets: None,
        });
    };
    let empty = Map::new();
    let track = track.as_object().unwrap_or(&empty);
    let field = |key: &str, default: &str| track.get(key).cloned().unwrap_or(json!(default));
    let images = track
        .get("images")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    // `images.get("coverarthq") or images.get("coverart")`
    let coverart = images
        .get("coverarthq")
        .filter(|v| truthy(v))
        .or(images.get("coverart"))
        .cloned()
        .unwrap_or(Value::Null);
    let result = json!({
        "title": field("title", "Unknown Title"),
        "artist": field("subtitle", "Unknown Artist"),
        "shazam_url": track.get("url").cloned().unwrap_or(Value::Null),
        "album": album_name(track),
        "coverart_url": coverart,
        "confidence": estimate_confidence(&response),
    });
    // Where in the matched song each match aligned: what offset prediction reads.
    let offsets = matches(&response)
        .iter()
        .filter(|m| py::number(m.get("offset")).is_some())
        .map(|m| {
            json!({
                "offset": m.get("offset").cloned().unwrap_or(Value::Null),
                "timeskew": m.get("timeskew").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    Ok(Heard {
        result: Some(result),
        offsets: Some(offsets),
    })
}

/// Python's truthiness for a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `result.get("matches") or []`, keeping only the objects.
fn matches(response: &Value) -> Vec<&Map<String, Value>> {
    response
        .get("matches")
        .and_then(Value::as_array)
        .map(|ms| ms.iter().filter_map(Value::as_object).collect())
        .unwrap_or_default()
}

/// The matched track's album. Every level is optional and turns up present but empty in the
/// wild (`"metadata": []`), which once threw away a real identification in Python.
fn album_name(track: &Map<String, Value>) -> Value {
    let first = |v: Option<&Value>| v.and_then(Value::as_array).and_then(|a| a.first()).cloned();
    first(track.get("sections"))
        .and_then(|s| first(s.get("metadata")))
        .and_then(|m| m.get("text").cloned())
        .unwrap_or(Value::Null)
}

/// A match-confidence proxy in [0, 1]: how well the matches align (low frequency skew) and
/// how many of them agree. Not a probability, but it ranks matches and separates a strong
/// one-off hit from a stray. Rounded as Python rounds, since the 0.6 singleton gate reads it.
pub fn estimate_confidence(response: &Value) -> f64 {
    let matches = matches(response);
    if matches.is_empty() {
        return 0.5;
    }
    let alignment: f64 = matches
        .iter()
        .map(|m| py::max(0.0, 1.0 - py::number_or_zero(m.get("frequencyskew")).abs()))
        .sum::<f64>()
        / matches.len() as f64;
    let corroboration = py::min(1.0, matches.len() as f64 / 3.0);
    py::round_to(0.5 * alignment + 0.5 * corroboration, 3)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(v: Value) -> Heard {
        parse_answer(200, v.to_string().as_bytes()).unwrap()
    }

    #[test]
    fn a_match_is_stored_as_python_stored_it() {
        let heard = parse(json!({
            "matches": [{"id": "1", "offset": 154.2, "timeskew": 0.0007, "frequencyskew": 0.001}],
            "track": {
                "title": "One More Time", "subtitle": "Daft Punk", "url": "u",
                "images": {"coverart": "small", "coverarthq": ""},
                "sections": [{"metadata": [{"text": "Discovery"}]}],
            },
        }));
        let result = heard.result.unwrap();
        // Key order matters: it is the progress file's.
        assert_eq!(
            serde_json::to_string(&result).unwrap(),
            r#"{"title":"One More Time","artist":"Daft Punk","shazam_url":"u","album":"Discovery","coverart_url":"small","confidence":0.666}"#
        );
        assert_eq!(
            heard.offsets.unwrap(),
            vec![json!({"offset": 154.2, "timeskew": 0.0007})]
        );
    }

    #[test]
    fn missing_fields_take_pythons_defaults() {
        let heard = parse(json!({"track": {"sections": [{"metadata": []}]}}));
        assert_eq!(
            heard.result.unwrap(),
            json!({"title": "Unknown Title", "artist": "Unknown Artist", "shazam_url": null,
                   "album": null, "coverart_url": null, "confidence": 0.5})
        );
        assert_eq!(heard.offsets, Some(vec![]));
    }

    #[test]
    fn only_numeric_offsets_are_kept() {
        let heard = parse(json!({
            "matches": [{"offset": "x"}, {"timeskew": 1}, {"offset": 3}],
            "track": {"title": "T", "subtitle": "A"},
        }));
        assert_eq!(
            heard.offsets.unwrap(),
            vec![json!({"offset": 3, "timeskew": null})]
        );
    }

    #[test]
    fn no_track_is_unidentified() {
        let none = Heard {
            result: None,
            offsets: None,
        };
        assert_eq!(parse(json!({"matches": [], "tagid": "x"})), none);
        assert_eq!(parse(json!({})), none);
    }

    #[test]
    fn a_throttle_or_a_failure_is_never_unidentified() {
        // The ~142-byte HTML page Shazam serves when it rate-limits.
        let page = b"<html><head><title>429 Too Many Requests</title></head></html>";
        assert_eq!(parse_answer(429, page), Err(NoAnswer::Throttled));
        assert!(matches!(parse_answer(200, page), Err(NoAnswer::Failed(_))));
        assert!(matches!(parse_answer(503, b"{}"), Err(NoAnswer::Failed(_))));
        // A JSON error body used to come back as a plain dict, i.e. "no track".
        assert!(matches!(
            parse_answer(400, br#"{"error": "bad signature"}"#),
            Err(NoAnswer::Failed(_))
        ));
    }

    #[test]
    fn confidence() {
        // Python's test_shazam_client cases.
        assert_eq!(estimate_confidence(&json!({"track": {}})), 0.5);
        let strong = estimate_confidence(&json!({"matches": [
            {"frequencyskew": 0.0}, {"frequencyskew": 0.01}, {"frequencyskew": 0.0}]}));
        let weak = estimate_confidence(&json!({"matches": [{"frequencyskew": 0.0}]}));
        assert!(strong > weak && (0.0..=1.0).contains(&weak) && strong <= 1.0);
        let skewed = estimate_confidence(&json!({"matches": [{"frequencyskew": 0.9}]}));
        assert!(skewed < weak);
        // Values from Python: round(0.5 * 0.99666... + 0.5, 3), and `or 0` on a null skew.
        assert_eq!(strong, 0.998);
        assert_eq!(
            estimate_confidence(&json!({"matches": [{"frequencyskew": null}, {}]})),
            0.833
        );
    }
}
