//! The router's own request and reply: just what the routes read and write, so the app's
//! custom scheme, `hindsight serve` and the tests all speak it without an HTTP stack.

use std::path::PathBuf;

#[derive(Clone, Debug, Default)]
pub struct Request {
    pub method: String,
    /// The path without the query, e.g. `/api/tracklist`.
    pub path: String,
    /// What follows `?`, undecoded.
    pub query: String,
    /// Header names in lower case.
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Request {
    pub fn get(target: &str) -> Request {
        Request::new("GET", target)
    }

    pub fn post_json(target: &str, body: impl Into<Vec<u8>>) -> Request {
        Request {
            body: body.into(),
            ..Request::new("POST", target)
        }
        .header("content-type", "application/json")
    }

    /// `target` is a path with an optional `?query`.
    pub fn new(method: &str, target: &str) -> Request {
        let (path, query) = target.split_once('?').unwrap_or((target, ""));
        Request {
            method: method.to_string(),
            path: path.to_string(),
            query: query.to_string(),
            ..Request::default()
        }
    }

    pub fn header(mut self, name: &str, value: &str) -> Request {
        self.headers
            .push((name.to_ascii_lowercase(), value.to_string()));
        self
    }

    pub fn header_value(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    /// The media type without parameters, lower case.
    pub fn content_type(&self) -> String {
        self.header_value("content-type")
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase()
    }

    /// A query parameter, percent-decoded (`+` is a space).
    pub fn param(&self, name: &str) -> Option<String> {
        self.query.split('&').find_map(|pair| {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            (decode(k) == name).then(|| decode(v))
        })
    }
}

fn decode(text: &str) -> String {
    let hex = |b: u8| (b as char).to_digit(16);
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let escaped = match bytes.get(i..i + 3) {
            Some([b'%', hi, lo]) => hex(*hi).zip(hex(*lo)).map(|(h, l)| (h * 16 + l) as u8),
            _ => None,
        };
        match (escaped, bytes[i]) {
            (Some(b), _) => {
                out.push(b);
                i += 3;
                continue;
            }
            (None, b'+') => out.push(b' '),
            (None, b) => out.push(b),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// What a reply carries: bytes, or a stretch of a file for the transport to stream (the
/// recording, which is far too big to hold).
#[derive(Debug, PartialEq)]
pub enum Body {
    Bytes(Vec<u8>),
    File { path: PathBuf, start: u64, len: u64 },
}

#[derive(Debug)]
pub struct Reply {
    pub status: u16,
    pub headers: Vec<(&'static str, String)>,
    pub body: Body,
}

impl Reply {
    pub fn bytes(status: u16, content_type: &str, body: Vec<u8>) -> Reply {
        Reply {
            status,
            headers: vec![("Content-Type", content_type.to_string())],
            body: Body::Bytes(body),
        }
    }

    pub fn json(status: u16, value: &serde_json::Value) -> Reply {
        Reply::bytes(status, "application/json", value.to_string().into_bytes())
    }

    pub fn ok(value: serde_json::Value) -> Reply {
        Reply::json(200, &value)
    }

    /// `{"ok": false, "error": …}`, the shape the page reads a failure from.
    pub fn error(status: u16, message: impl std::fmt::Display) -> Reply {
        Reply::json(
            status,
            &serde_json::json!({"ok": false, "error": message.to_string()}),
        )
    }

    /// A plain-text error, as Python's `send_error` (the page reads only the status).
    pub fn status(status: u16, message: &str) -> Reply {
        Reply::bytes(
            status,
            "text/plain; charset=utf-8",
            message.as_bytes().to_vec(),
        )
    }

    pub fn with(mut self, name: &'static str, value: impl Into<String>) -> Reply {
        self.headers.push((name, value.into()));
        self
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    /// The body as bytes (tests); a file stretch is read.
    pub fn body_bytes(&self) -> Vec<u8> {
        match &self.body {
            Body::Bytes(b) => b.clone(),
            Body::File { path, start, len } => {
                use std::io::{Read, Seek, SeekFrom};
                let mut f = std::fs::File::open(path).expect("reply file");
                f.seek(SeekFrom::Start(*start)).expect("seek");
                let mut out = vec![0; *len as usize];
                f.read_exact(&mut out).expect("read");
                out
            }
        }
    }

    pub fn body_json(&self) -> serde_json::Value {
        serde_json::from_slice(&self.body_bytes()).expect("a JSON body")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_parameters_are_decoded() {
        let r =
            Request::get("/api/artwork/options?index=3&artist=Sly+%26+the&title=Caf%C3%A9&x=%zz");
        assert_eq!(r.path, "/api/artwork/options");
        assert_eq!(r.param("index").as_deref(), Some("3"));
        assert_eq!(r.param("artist").as_deref(), Some("Sly & the"));
        assert_eq!(r.param("title").as_deref(), Some("Café"));
        assert_eq!(r.param("x").as_deref(), Some("%zz"));
        assert_eq!(r.param("missing"), None);
    }
}
