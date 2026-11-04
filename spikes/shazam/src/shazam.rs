//! One request to Shazam's tag endpoint, mirroring shazamio 0.8.1's
//! `send_recognize_request_v2` (URL, headers and body) so the two are comparable.

use serde_json::{json, Value};
use std::time::{SystemTime, UNIX_EPOCH};

const URL: &str = "https://amp.shazam.com/discovery/v5/en-US/GB/{device}/-/tag/{u1}/{u2}\
?sync=true&webv3=true&sampling=true&connected=&shazamapiversion=v3&sharehub=true\
&hubv5minorversion=v5.1&hidelb=true&video=v3";
const USER_AGENT: &str = "Dalvik/2.1.0 (Linux; U; Android 5.0.2; VS980 4G Build/LRX22G)";

pub struct Answer {
    pub status: u16,
    pub artist: Option<String>,
    pub title: Option<String>,
    pub offsets: Vec<f64>,
}

pub fn recognize(uri: &str, excerpt_ms: u32) -> Result<Answer, Box<dyn std::error::Error>> {
    let u1 = uuid::Uuid::new_v4();
    let device = ["iphone", "android", "web"][(u1.as_bytes()[0] % 3) as usize];
    let url = URL
        .replace("{device}", device)
        .replace("{u1}", &u1.to_string().to_uppercase())
        .replace("{u2}", &uuid::Uuid::new_v4().to_string().to_uppercase());
    let now_ms = SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis() as u32;
    let body = json!({
        "timezone": "Europe/Moscow",
        "signature": {"uri": uri, "samplems": excerpt_ms},
        "timestamp": now_ms,
        "context": {},
        "geolocation": {},
    });
    let resp = reqwest::blocking::Client::new()
        .post(url)
        .header("X-Shazam-Platform", "IPHONE")
        .header("X-Shazam-AppVersion", "14.1.0")
        .header("Accept", "*/*")
        .header("Accept-Language", "en-US")
        .header("Accept-Encoding", "gzip, deflate")
        .header("User-Agent", USER_AGENT)
        .json(&body)
        .send()?;
    let status = resp.status().as_u16();
    let text = resp.text()?;
    // A throttled request comes back as a small HTML page; that must not read as "no match".
    let v: Value = serde_json::from_str(&text)
        .map_err(|e| format!("HTTP {status}, body is not JSON ({e}): {:.200}", text))?;
    let track = &v["track"];
    Ok(Answer {
        status,
        artist: track["subtitle"].as_str().map(String::from),
        title: track["title"].as_str().map(String::from),
        offsets: v["matches"]
            .as_array()
            .map(|ms| ms.iter().filter_map(|m| m["offset"].as_f64()).collect())
            .unwrap_or_default(),
    })
}
