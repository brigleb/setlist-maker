//! `hindsight serve [<set>]` (hidden): the app's editor behind a loopback port, for the
//! headless browser checks, which can't drive the app's own webview. Ctrl-C stops it.
//!
//! The app has no socket, so it has no host guard; this does. Binding 127.0.0.1 only stops
//! other machines: a page the user already has open can point its own name at 127.0.0.1 (DNS
//! rebinding), and the browser then treats this server as that site's, letting it read the
//! recording and post saves. A rebound request still carries the attacker's Host, so only the
//! loopback names with this exact port are served (#26).

use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

use hindsight_core::model::uploads::MAX_UPLOAD_BYTES;
use hindsight_editor::{Body, Config, Editor, EndRun, Reply, Request};

#[derive(clap::Args)]
pub struct Args {
    /// A recording or a `_tracklist.md` to open; without one, the drop screen.
    pub path: Option<PathBuf>,
    /// The port; 0 picks a free one.
    #[arg(long, default_value_t = 0)]
    pub port: u16,
}

/// Whether `host` (the Host header) names this server: a loopback name and exactly `port`.
pub fn host_allowed(host: &str, port: u16) -> bool {
    let Some((name, p)) = host.rsplit_once(':') else {
        return false;
    };
    p == port.to_string()
        && ["127.0.0.1", "localhost"]
            .iter()
            .any(|n| n.eq_ignore_ascii_case(name))
}

pub fn run(args: Args) -> ExitCode {
    crate::adopt_python_folders();
    let mut config = match Config::standard() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Error: {e}");
            return ExitCode::FAILURE;
        }
    };
    config.find_claude = Box::new(|| crate::find_on_path("claude"));
    let editor = Editor::new(config);
    if let Some(path) = &args.path
        && let Err(e) = editor.open(path, EndRun::Finish)
    {
        eprintln!("Error: {e}");
        return ExitCode::FAILURE;
    }
    let server = match tiny_http::Server::http(("127.0.0.1", args.port)) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("Error: can't listen on 127.0.0.1:{}: {e}", args.port);
            return ExitCode::FAILURE;
        }
    };
    let port = server.server_addr().to_ip().map_or(args.port, |a| a.port());
    println!("http://127.0.0.1:{port}/");
    for request in server.incoming_requests() {
        let editor = editor.clone();
        std::thread::spawn(move || answer(&editor, request, port));
    }
    ExitCode::SUCCESS
}

fn answer(editor: &Arc<Editor>, mut request: tiny_http::Request, port: u16) {
    let headers: Vec<(String, String)> = request
        .headers()
        .iter()
        .map(|h| {
            (
                h.field.as_str().as_str().to_ascii_lowercase(),
                h.value.as_str().to_string(),
            )
        })
        .collect();
    let host = headers
        .iter()
        .find(|(n, _)| n == "host")
        .map(|(_, v)| v.as_str());
    if !host.is_some_and(|h| host_allowed(h, port)) {
        let _ = request
            .respond(tiny_http::Response::from_string("invalid Host header").with_status_code(403));
        return;
    }
    let declared: u64 = headers
        .iter()
        .find(|(n, _)| n == "content-length")
        .and_then(|(_, v)| v.trim().parse().ok())
        .unwrap_or(0);
    let mut body = Vec::new();
    if declared > (MAX_UPLOAD_BYTES + (1 << 20)) as u64 {
        // Drained and dropped, never held: the route answers 413 from the header, and the
        // client sees that rather than a reset connection.
        let _ = std::io::copy(&mut request.as_reader(), &mut std::io::sink());
    } else {
        let _ = request.as_reader().read_to_end(&mut body);
    }
    let url = request.url().to_string();
    let mut req = Request::new(request.method().as_str(), &url);
    req.headers = headers;
    req.body = body;
    let reply = editor.handle(&req);
    let _ = request.respond(response(reply));
}

fn response(reply: Reply) -> tiny_http::Response<Box<dyn Read + Send>> {
    let headers: Vec<tiny_http::Header> = reply
        .headers
        .iter()
        .filter(|(n, _)| !n.eq_ignore_ascii_case("content-length"))
        .filter_map(|(n, v)| tiny_http::Header::from_bytes(n.as_bytes(), v.as_bytes()).ok())
        .collect();
    let (reader, len): (Box<dyn Read + Send>, usize) = match reply.body {
        Body::Bytes(b) => {
            let len = b.len();
            (Box::new(std::io::Cursor::new(b)), len)
        }
        Body::File { path, start, len } => match std::fs::File::open(&path).and_then(|mut f| {
            f.seek(SeekFrom::Start(start))?;
            Ok(f)
        }) {
            Ok(f) => (Box::new(f.take(len)), len as usize),
            Err(_) => (Box::new(std::io::empty()), 0),
        },
    };
    tiny_http::Response::new(
        tiny_http::StatusCode(reply.status),
        headers,
        reader,
        Some(len),
        None,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_this_servers_loopback_names_and_port_are_served() {
        // Python's test_every_endpoint_rejects_a_foreign_host and its neighbours.
        for good in ["127.0.0.1:8123", "localhost:8123", "LOCALHOST:8123"] {
            assert!(host_allowed(good, 8123), "{good}");
        }
        for bad in [
            "evil.example:8123",
            "127.0.0.1",
            "127.0.0.1:80",
            "localhost:81234",
            "127.0.0.1.evil.example:8123",
            "",
            "[::1]:8123",
        ] {
            assert!(!host_allowed(bad, 8123), "{bad}");
        }
    }
}
