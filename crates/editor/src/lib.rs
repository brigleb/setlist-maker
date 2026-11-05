//! The editor's backend: every route the page calls, for one window's set. The app serves it
//! through its `hindsight://` scheme and `hindsight serve` through a loopback port; both hand
//! [`Request`]s to [`Editor::handle`] and send back its [`Reply`].

pub mod api;
mod assets;
mod claude;
mod http;
mod range;
mod routes;
mod session;
#[cfg(test)]
mod tests;

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::time::Duration;

use hindsight_core::artwork::{ArtworkCache, Fetch, Web, cache};
use hindsight_core::audio::peaks;
use hindsight_core::model::fileio::config_dir;
use hindsight_core::run::BACKOFF;

pub use claude::find_claude;
pub use http::{Body, Reply, Request};
pub use session::{EndRun, ListenFor, OpenListener, Opening, classify, shazam_listener};

use session::Phase;

/// Everything the editor reaches outside itself, so a test can swap any of it.
pub struct Config {
    /// `corrections.json` and `config.json` (the remembered episode artist).
    pub config_dir: PathBuf,
    pub artwork: ArtworkCache<Box<dyn Fetch>>,
    pub peaks_dir: PathBuf,
    pub listener: Box<OpenListener>,
    /// Between Shazam calls, and the waits before each retry.
    pub delay: Duration,
    pub backoff: Vec<Duration>,
    /// The most audio one reply carries. The app's webview takes each reply as one buffer, so
    /// it caps (and always asks for ranges anyway); `serve` streams and doesn't.
    pub audio_cap: Option<u64>,
    /// Finds the `claude` CLI, for the Set tab's Generate. Run once, off the request path.
    pub find_claude: Box<dyn Fn() -> Option<PathBuf> + Send + Sync>,
    /// Closes the window (the app); `serve` has none.
    pub on_close: Box<dyn Fn() + Send + Sync>,
}

impl Config {
    /// The real folders, the web and Shazam.
    pub fn standard() -> Result<Config, String> {
        let web = Web::new().ok_or("can't start an HTTP client")?;
        Ok(Config {
            config_dir: config_dir(),
            artwork: ArtworkCache::new(cache::default_dir(), Box::new(web) as Box<dyn Fetch>),
            peaks_dir: peaks::default_dir(),
            listener: Box::new(shazam_listener),
            delay: session::DELAY,
            backoff: BACKOFF.to_vec(),
            audio_cap: None,
            find_claude: Box::new(|| None),
            on_close: Box::new(|| {}),
        })
    }
}

pub(crate) struct State {
    pub phase: Phase,
    /// Bumped whenever which index names which track changes: an insert or a moved start
    /// re-sorts the list, a finished run or another open replaces it. A save made against an
    /// older revision is refused, its indices now name other tracks (#64).
    pub revision: u64,
    pub live_runs: u64,
    /// Why the last open or run failed, for the drop screen.
    pub notice: Option<String>,
    /// A path the Dock icon or Finder handed the app, waiting for the page to ask for it
    /// (a cold launch's arrives before the page has loaded).
    pub pending: Option<PathBuf>,
}

/// One window's editor.
pub struct Editor {
    pub(crate) config: Config,
    state: Mutex<State>,
    /// Held by saves and embeds, which both write the set's files from its tracklist.
    pub(crate) writing: Mutex<()>,
    claude: OnceLock<Option<PathBuf>>,
    /// When this window last asked Shazam (a run or a click), so a new sampler waits its turn.
    last_shazam: Mutex<Option<std::time::Instant>>,
}

impl Editor {
    pub fn new(config: Config) -> Arc<Editor> {
        let editor = Arc::new(Editor {
            config,
            state: Mutex::new(State {
                phase: Phase::Empty,
                revision: 0,
                live_runs: 0,
                notice: None,
                pending: None,
            }),
            writing: Mutex::new(()),
            claude: OnceLock::new(),
            last_shazam: Mutex::new(None),
        });
        // Looked for now, so the Set tab knows by the time anyone opens it.
        let e = Arc::downgrade(&editor);
        std::thread::spawn(move || {
            if let Some(e) = e.upgrade() {
                e.claude();
            }
        });
        editor
    }

    pub(crate) fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub(crate) fn last_shazam(&self) -> MutexGuard<'_, Option<std::time::Instant>> {
        self.last_shazam.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub(crate) fn claude(&self) -> Option<&Path> {
        self.claude
            .get_or_init(|| (self.config.find_claude)())
            .as_deref()
    }

    /// A path from outside the page (the Dock icon, Finder's Open With) for the page to open,
    /// after asking about whatever is open now.
    pub fn offer(&self, path: PathBuf) {
        self.state().pending = Some(path);
    }

    /// Whether a run is identifying, so closing the window asks first.
    pub fn is_live(&self) -> bool {
        matches!(self.state().phase, Phase::Live(_))
    }
}
