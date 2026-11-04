//! M0 spike 3: the existing editor page in a Tauri window through `hindsight://`, the set's MP3
//! streamed to `<audio>` by Range requests, and the two ways a file arrives (a window drop, and
//! the Dock icon / `open -a`). Every request and event goes to the log; that log is the result.
//!
//! Paths come from the environment at run time, else at build time (a bundle launched by
//! `open` gets no environment): SPIKE_SET (a set folder: its .mp3 is served), SPIKE_FIXTURES
//! (`api/{tracklist,probes,peaks}` captured from Python's server), SPIKE_LOG, SPIKE_CAP_MB
//! (per-response cap, default 4) and SPIKE_AUTOTEST=1 (play and seek from inside the page).

mod range;

use range::Slice;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tauri::http::{header, Request, Response, StatusCode};
use tauri::{DragDropEvent, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const WEB: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../setlist_maker");
const SPIKE_JS: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/spike");

fn setting(name: &str, built: Option<&'static str>) -> Option<String> {
    std::env::var(name).ok().or(built.map(String::from))
}

struct Log {
    t0: Instant,
    file: Mutex<File>,
}

impl Log {
    fn line(&self, msg: impl AsRef<str>) {
        let ms = self.t0.elapsed().as_secs_f64() * 1000.0;
        let line = format!("{ms:10.1}\t{}\n", msg.as_ref());
        eprint!("{line}");
        let _ = self.file.lock().unwrap().write_all(line.as_bytes());
    }
}

struct Spike {
    log: Log,
    audio: Mutex<Option<PathBuf>>,
    fixtures: Option<PathBuf>,
    cap: u64,
    autotest: bool,
}

fn is_mp3(p: &Path) -> bool {
    p.extension().is_some_and(|e| e.eq_ignore_ascii_case("mp3"))
}

/// A plain file name (no separators, no `..`) with one of `exts`: all a request may name.
fn plain_name<'a>(name: &'a str, exts: &[&str]) -> Option<&'a str> {
    let ok = !name.is_empty()
        && !name.starts_with('.')
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c))
        && exts.iter().any(|e| name.ends_with(e));
    ok.then_some(name)
}

fn first_mp3(dir: &Path) -> Option<PathBuf> {
    std::fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .find(|p| is_mp3(p))
}

fn mime(path: &str) -> &'static str {
    match path.rsplit('.').next() {
        Some("html") => "text/html; charset=utf-8",
        Some("js") => "text/javascript; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn respond(status: StatusCode, ctype: &str, body: Vec<u8>) -> Response<Vec<u8>> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, ctype)
        .header(header::CACHE_CONTROL, "no-store")
        .body(body)
        .unwrap()
}

fn not_found() -> Response<Vec<u8>> {
    respond(StatusCode::NOT_FOUND, "text/plain", b"not found".to_vec())
}

fn file(path: &Path, ctype: &str) -> Response<Vec<u8>> {
    match std::fs::read(path) {
        Ok(b) => respond(StatusCode::OK, ctype, b),
        Err(_) => not_found(),
    }
}

fn page(spike: &Spike) -> Response<Vec<u8>> {
    let Ok(html) = std::fs::read_to_string(Path::new(WEB).join("web_editor.html")) else {
        return not_found();
    };
    let mut tags = String::from(r#"<script src="/spike/hooks.js"></script>"#);
    if spike.autotest {
        tags.push_str(r#"<script type="module" src="/spike/autotest.js"></script>"#);
    }
    let html = html.replacen("</body>", &format!("{tags}\n</body>"), 1);
    respond(StatusCode::OK, mime(".html"), html.into_bytes())
}

/// `/api/audio`: a ranged answer is at most `cap` bytes, since the body is one `Vec`. A request
/// with no usable Range gets the whole file in one `Vec`; WebKit never sent one (spike 3).
fn audio(spike: &Spike, req: &Request<Vec<u8>>) -> (Response<Vec<u8>>, String) {
    let Some(path) = spike.audio.lock().unwrap().clone() else {
        return (not_found(), "no audio".into());
    };
    let Ok(mut f) = File::open(&path) else {
        return (not_found(), format!("can't open {}", path.display()));
    };
    let size = f.metadata().map(|m| m.len()).unwrap_or(0);
    let asked = req
        .headers()
        .get(header::RANGE)
        .and_then(|v| v.to_str().ok());
    let b = Response::builder()
        .header(header::CONTENT_TYPE, "audio/mpeg")
        .header(header::ACCEPT_RANGES, "bytes")
        .header(header::CACHE_CONTROL, "no-store");
    let (start, len, b) = match range::slice(asked, size, spike.cap) {
        Slice::Full { len } => (0, len, b.status(StatusCode::OK)),
        Slice::Partial { start, end, size } => (
            start,
            end - start + 1,
            b.status(StatusCode::PARTIAL_CONTENT)
                .header(header::CONTENT_RANGE, format!("bytes {start}-{end}/{size}")),
        ),
        Slice::Unsatisfiable { size } => {
            let r = b
                .status(StatusCode::RANGE_NOT_SATISFIABLE)
                .header(header::CONTENT_RANGE, format!("bytes */{size}"))
                .body(Vec::new())
                .unwrap();
            return (r, format!("range={asked:?} -> 416"));
        }
    };
    let mut body = vec![0; len as usize];
    if f.seek(SeekFrom::Start(start))
        .and_then(|_| f.read_exact(&mut body))
        .is_err()
    {
        return (not_found(), "read failed".into());
    }
    let r = b.header(header::CONTENT_LENGTH, len).body(body).unwrap();
    let note = format!("range={asked:?} -> {} {start}+{len}", r.status().as_u16());
    (r, note)
}

fn rss_kb(pid: &str) -> String {
    std::process::Command::new("ps")
        .args(["-o", "rss=", "-p", pid])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default()
}

fn handle(spike: &Spike, req: Request<Vec<u8>>) -> Response<Vec<u8>> {
    let t = Instant::now();
    let path = req.uri().path().to_string();
    let (resp, note) = match path.as_str() {
        "/" => (page(spike), String::new()),
        "/timeline.js" => (
            file(&Path::new(WEB).join("web_timeline.js"), mime(".js")),
            String::new(),
        ),
        p if p.starts_with("/web/") => {
            let r = match plain_name(&p[5..], &[".js", ".css"]) {
                Some(name) => file(&Path::new(WEB).join("web").join(name), mime(name)),
                None => not_found(),
            };
            (r, String::new())
        }
        p if p.starts_with("/spike/") && p.ends_with(".js") => {
            let r = match plain_name(&p[7..], &[".js"]) {
                Some(name) => file(&Path::new(SPIKE_JS).join(name), mime(".js")),
                None => not_found(),
            };
            (r, String::new())
        }
        "/spike/report" => {
            let body = String::from_utf8_lossy(req.body()).to_string();
            let pid = std::process::id().to_string();
            spike.log.line(format!("REPORT {body}"));
            spike.log.line(format!("RSS app={}kB", rss_kb(&pid)));
            let mut r = respond(StatusCode::NO_CONTENT, "text/plain", Vec::new());
            r.headers_mut()
                .insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*".parse().unwrap());
            (r, String::new())
        }
        "/api/audio" => audio(spike, &req),
        "/api/tracklist" | "/api/probes" | "/api/peaks" => {
            let name = &path[5..];
            let ctype = if name == "peaks" {
                "application/octet-stream"
            } else {
                "application/json"
            };
            let r = match &spike.fixtures {
                Some(d) => file(&d.join("api").join(name), ctype),
                None => not_found(),
            };
            (r, String::new())
        }
        _ => (not_found(), String::new()),
    };
    if !path.starts_with("/spike/report") {
        spike.log.line(format!(
            "{} {} {} {:.1}ms {}",
            req.method(),
            req.uri(),
            resp.status().as_u16(),
            t.elapsed().as_secs_f64() * 1000.0,
            note
        ));
    }
    resp
}

/// A file arrived (window drop or Dock / `open -a`): log it, and if it's an MP3, load it (no
/// autoplay: this runs on the real speakers). On a cold launch the window isn't there yet.
fn arrived(app: &tauri::AppHandle, how: &str, paths: Vec<PathBuf>) {
    let spike = app.state::<Arc<Spike>>();
    spike.log.line(format!("{how} {paths:?}"));
    if let Some(mp3) = paths.into_iter().find(|p| is_mp3(p)) {
        *spike.audio.lock().unwrap() = Some(mp3);
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.eval(
                "{const a=document.getElementById('audio'); a.src='/api/audio?v='+Date.now(); \
                 a.addEventListener('loadedmetadata', () => fetch('/spike/report', {method: 'POST', \
                 body: JSON.stringify({kind: 'loaded-after-drop', duration: a.duration})}), {once: true})}",
            );
        }
    }
}

fn main() {
    let log_path = setting("SPIKE_LOG", option_env!("SPIKE_LOG")).unwrap_or_else(|| {
        std::env::temp_dir()
            .join("hindsight-spike3.log")
            .display()
            .to_string()
    });
    let set = setting("SPIKE_SET", option_env!("SPIKE_SET")).map(PathBuf::from);
    let spike = Arc::new(Spike {
        log: Log {
            t0: Instant::now(),
            file: Mutex::new(
                OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&log_path)
                    .unwrap(),
            ),
        },
        audio: Mutex::new(set.as_deref().and_then(first_mp3)),
        fixtures: setting("SPIKE_FIXTURES", option_env!("SPIKE_FIXTURES")).map(PathBuf::from),
        cap: setting("SPIKE_CAP_MB", option_env!("SPIKE_CAP_MB"))
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(4)
            << 20,
        autotest: setting("SPIKE_AUTOTEST", option_env!("SPIKE_AUTOTEST")).as_deref() == Some("1"),
    });
    spike.log.line(format!(
        "START pid={} args={:?} audio={:?} cap={} autotest={}",
        std::process::id(),
        std::env::args().collect::<Vec<_>>(),
        spike.audio.lock().unwrap(),
        spike.cap,
        spike.autotest
    ));

    let for_scheme = spike.clone();
    let app = tauri::Builder::default()
        .manage(spike)
        .register_asynchronous_uri_scheme_protocol("hindsight", move |_ctx, req, responder| {
            let spike = for_scheme.clone();
            std::thread::spawn(move || responder.respond(handle(&spike, req)));
        })
        .setup(|app| {
            // SPIKE_URL: load Python's http:// server instead, in this same webview (the A/B).
            let (url, external) = match std::env::var("SPIKE_URL") {
                Ok(u) => (WebviewUrl::External(u.parse()?), true),
                Err(_) => (
                    WebviewUrl::CustomProtocol("hindsight://localhost/".parse()?),
                    false,
                ),
            };
            let mut w = WebviewWindowBuilder::new(app, "main", url)
                .title("Hindsight spike 3")
                .inner_size(1400.0, 900.0);
            if external && app.state::<Arc<Spike>>().autotest {
                w = w.initialization_script(include_str!("../spike/autotest.js"));
            }
            w.build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::DragDrop(DragDropEvent::Drop { paths, position }) = event {
                arrived(
                    window.app_handle(),
                    &format!("WINDOW-DROP at {position:?}"),
                    paths.clone(),
                );
            }
        })
        .build(tauri::generate_context!())
        .expect("build the app");

    app.run(|app, event| {
        #[cfg(target_os = "macos")]
        if let RunEvent::Opened { urls } = event {
            let paths = urls.iter().filter_map(|u| u.to_file_path().ok()).collect();
            arrived(app, &format!("OPENED urls={urls:?} ->"), paths);
        }
        let _ = app; // used only by the macOS arm above
    });
}
