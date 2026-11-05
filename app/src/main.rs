//! Hindsight, the app: one window showing the editor page through `hindsight://`, every
//! request answered by `hindsight-editor`. No socket, so no host guard (spike 3).
//!
//! What arrives from outside the page:
//! - a file dropped on the window: the page hears `tauri://drag-drop` itself, works out what
//!   was under the pointer, and asks the editor to open it or upload it;
//! - a file dropped on the Dock icon, or opened with the app from Finder: held by the editor
//!   until the page asks for it (`/api/pending`), since on a cold launch it arrives before the
//!   page has loaded, and the page is told with `hindsight://open`;
//! - closing the window or quitting: the page is told with `hindsight://close` and asks the
//!   user (Save & close / Discard / Cancel, or Finish now / Keep running / Stop), then posts
//!   `/api/close`. A second close within two seconds goes through regardless, so a page that
//!   can't answer never traps the window.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::http::{Response, StatusCode};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

use hindsight_editor::{Body, Config, Editor, Reply, Request, find_claude};

/// The most audio one reply carries: the webview gets each reply as one buffer, and asks
/// for 64–145KB at a time anyway (spike 3).
const AUDIO_CAP: u64 = 4 << 20;

/// A second close this soon after the first isn't waiting for the page.
const FORCE_CLOSE: Duration = Duration::from_secs(2);

struct Warnings;

impl log::Log for Warnings {
    fn enabled(&self, m: &log::Metadata) -> bool {
        m.level() <= log::Level::Warn
    }
    fn log(&self, r: &log::Record) {
        if self.enabled(r.metadata()) {
            eprintln!("Warning: {}", r.args());
        }
    }
    fn flush(&self) {}
}

/// The first launch copies what Python learned, as the CLI's does.
fn adopt_python_folders() {
    use hindsight_core::model::fileio::{
        adopt_python_cache, adopt_python_config, cache_dir, config_dir,
    };
    for (base, adopt) in [
        (
            config_dir(),
            adopt_python_config as fn(&std::path::Path) -> std::io::Result<bool>,
        ),
        (cache_dir(), adopt_python_cache),
    ] {
        if let Some(base) = base.parent()
            && let Err(e) = adopt(base)
        {
            log::warn!("couldn't copy setlist-maker's {}: {e}", base.display());
        }
    }
}

fn to_request(req: &tauri::http::Request<Vec<u8>>) -> Request {
    let target = match req.uri().query() {
        Some(q) => format!("{}?{q}", req.uri().path()),
        None => req.uri().path().to_string(),
    };
    let mut r = Request::new(req.method().as_str(), &target);
    r.headers = req
        .headers()
        .iter()
        .filter_map(|(n, v)| {
            Some((
                n.as_str().to_ascii_lowercase(),
                v.to_str().ok()?.to_string(),
            ))
        })
        .collect();
    r.body = req.body().clone();
    r
}

fn to_response(reply: Reply) -> Response<Vec<u8>> {
    let body = match reply.body {
        Body::Bytes(b) => Ok(b),
        // Capped at AUDIO_CAP by the editor, so one buffer is fine.
        Body::File { path, start, len } => std::fs::File::open(&path).and_then(|mut f| {
            f.seek(SeekFrom::Start(start))?;
            let mut out = vec![0; len as usize];
            f.read_exact(&mut out)?;
            Ok(out)
        }),
    };
    let Ok(body) = body else {
        return Response::builder()
            .status(StatusCode::NOT_FOUND)
            .body(b"not found".to_vec())
            .expect("a plain response");
    };
    let mut b = Response::builder().status(reply.status);
    for (name, value) in &reply.headers {
        b = b.header(*name, value);
    }
    b.body(body).unwrap_or_else(|_| {
        Response::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .body(Vec::new())
            .expect("a plain response")
    })
}

fn main() {
    if log::set_logger(&Warnings).is_ok() {
        log::set_max_level(log::LevelFilter::Warn);
    }
    adopt_python_folders();

    let handle: Arc<OnceLock<AppHandle>> = Arc::new(OnceLock::new());
    let closing = Arc::new(AtomicBool::new(false));
    let mut config = match Config::standard() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Error: {e}");
            std::process::exit(1);
        }
    };
    config.audio_cap = Some(AUDIO_CAP);
    config.find_claude = Box::new(find_claude);
    let (h, c) = (handle.clone(), closing.clone());
    config.on_close = Box::new(move || {
        c.store(true, Ordering::SeqCst);
        if let Some(w) = h.get().and_then(|app| app.get_webview_window("main")) {
            // destroy(), not close(): close() would ask the page again.
            let _ = w.destroy();
        }
    });
    let editor = Editor::new(config);

    let for_scheme = editor.clone();
    let asked_at: Arc<Mutex<Option<Instant>>> = Arc::new(Mutex::new(None));
    let asked = asked_at.clone();
    let app = tauri::Builder::default()
        .register_asynchronous_uri_scheme_protocol("hindsight", move |_ctx, req, responder| {
            let editor = for_scheme.clone();
            // A thread per request: a sample can wait out Shazam's pacing, an embed takes
            // seconds, and the audio must keep flowing meanwhile.
            std::thread::spawn(move || {
                let reply = editor.handle(&to_request(&req));
                responder.respond(to_response(reply));
            });
        })
        .setup(|app| {
            WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::CustomProtocol("hindsight://localhost/".parse()?),
            )
            .title("Hindsight")
            .inner_size(1440.0, 900.0)
            .min_inner_size(720.0, 520.0)
            .build()?;
            Ok(())
        })
        .on_window_event(move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let mut at = asked.lock().unwrap_or_else(|e| e.into_inner());
                if at.is_some_and(|t| t.elapsed() < FORCE_CLOSE) {
                    return; // closes
                }
                *at = Some(Instant::now());
                api.prevent_close();
                let _ = window.emit("hindsight://close", ());
            }
        })
        .build(tauri::generate_context!())
        .expect("build the app");
    let _ = handle.set(app.handle().clone());

    app.run(move |app, event| match event {
        #[cfg(target_os = "macos")]
        RunEvent::Opened { urls } => {
            let paths: Vec<PathBuf> = urls.iter().filter_map(|u| u.to_file_path().ok()).collect();
            if let Some(path) = paths.into_iter().next() {
                editor.offer(path);
                let _ = app.emit("hindsight://open", ());
            }
        }
        RunEvent::ExitRequested { api, code, .. } => {
            // ⌘Q with the window still up: ask the page, as closing the window does.
            if code.is_none()
                && !closing.load(Ordering::SeqCst)
                && let Some(w) = app.get_webview_window("main")
            {
                api.prevent_exit();
                let _ = w.emit("hindsight://close", ());
            }
        }
        _ => {}
    });
}
