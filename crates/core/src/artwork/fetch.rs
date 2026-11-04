//! The one door artwork goes out through, so the sources can be tested against canned answers.

use std::time::Duration;

use super::is_fetchable_url;

/// `User-Agent` for every artwork request. MusicBrainz asks for a contact URL in it.
pub const USER_AGENT: &str = concat!(
    "hindsight/",
    env!("CARGO_PKG_VERSION"),
    " (https://github.com/brigleb/setlist-maker)"
);

/// How long one request may take, as Python's `urlopen(timeout=15)`.
pub const TIMEOUT: Duration = Duration::from_secs(15);

/// A GET that either succeeds with a 2xx or is `None`: every failure (refused URL, network,
/// status, a body that won't read) means the same thing to a source, "nothing here".
pub trait Fetch: Send + Sync {
    /// The response body.
    fn get(&self, url: &str) -> Option<Vec<u8>>;
    /// Where the URL ends up after redirects, without reading the body. Cover Art Archive
    /// answers whether a release has a cover only by redirecting to it.
    fn final_url(&self, url: &str) -> Option<String>;
}

/// The real web, through a blocking reqwest client.
pub struct Web {
    http: reqwest::blocking::Client,
}

impl Web {
    pub fn new() -> Option<Web> {
        let http = reqwest::blocking::Client::builder()
            .user_agent(USER_AGENT)
            .timeout(TIMEOUT)
            .build()
            .ok()?;
        Some(Web { http })
    }

    fn send(&self, url: &str) -> Option<reqwest::blocking::Response> {
        // Refused here rather than at the UI, so every caller is covered, including a URL that
        // reached the sidecar some other way.
        if !is_fetchable_url(url) {
            log::debug!("refusing to fetch non-HTTP artwork URL {url:?}");
            return None;
        }
        let response = self
            .http
            .get(url)
            .header(
                reqwest::header::ACCEPT,
                "application/json, image/*;q=0.9, */*;q=0.8",
            )
            .send()
            .and_then(|r| r.error_for_status());
        match response {
            Ok(r) => Some(r),
            Err(e) => {
                log::debug!("artwork request failed for {url}: {e}");
                None
            }
        }
    }
}

impl Fetch for Web {
    fn get(&self, url: &str) -> Option<Vec<u8>> {
        self.send(url)?.bytes().ok().map(|b| b.to_vec())
    }

    fn final_url(&self, url: &str) -> Option<String> {
        self.send(url).map(|r| r.url().to_string())
    }
}

#[cfg(test)]
pub mod fake {
    //! Canned answers by URL, and a record of what was asked.

    use std::collections::HashMap;
    use std::sync::Mutex;

    use super::Fetch;

    #[derive(Default)]
    pub struct Canned {
        pub bodies: HashMap<String, Vec<u8>>,
        pub redirects: HashMap<String, String>,
        pub asked: Mutex<Vec<String>>,
    }

    impl Canned {
        pub fn body(mut self, url: &str, body: impl Into<Vec<u8>>) -> Self {
            self.bodies.insert(url.into(), body.into());
            self
        }

        pub fn redirect(mut self, url: &str, to: &str) -> Self {
            self.redirects.insert(url.into(), to.into());
            self
        }

        pub fn asked(&self) -> Vec<String> {
            self.asked.lock().unwrap().clone()
        }

        fn note(&self, url: &str) {
            self.asked.lock().unwrap().push(url.to_string());
        }
    }

    impl Fetch for Canned {
        fn get(&self, url: &str) -> Option<Vec<u8>> {
            self.note(url);
            self.bodies.get(url).cloned()
        }

        fn final_url(&self, url: &str) -> Option<String> {
            self.note(url);
            self.redirects.get(url).cloned()
        }
    }
}
