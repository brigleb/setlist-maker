//! The page, embedded: `web_editor.html`, `web_timeline.js` and `web/`. Until M7 moves them
//! to `web/` at the top, they're Python's files, shared, so both editors serve one page.

use include_dir::{Dir, include_dir};

use crate::http::Reply;

static WEB: Dir = include_dir!("$CARGO_MANIFEST_DIR/../../setlist_maker/web");
const PAGE: &str = include_str!("../../../setlist_maker/web_editor.html");
const TIMELINE: &str = include_str!("../../../setlist_maker/web_timeline.js");

const JS: &str = "text/javascript; charset=utf-8";

pub fn page() -> Reply {
    Reply::bytes(200, "text/html; charset=utf-8", PAGE.as_bytes().to_vec())
}

pub fn timeline() -> Reply {
    Reply::bytes(200, JS, TIMELINE.as_bytes().to_vec())
}

/// `/web/<name>.js` or `.css`: a plain lower-case name and a known extension, as Python's
/// `_WEB_ASSET`, so a request can't walk out of the folder or reach its LICENSES.md.
pub fn web(path: &str) -> Option<Reply> {
    let file = path.strip_prefix("/web/")?;
    let (name, ext) = file.rsplit_once('.')?;
    if name.is_empty() || !name.bytes().all(|b| b.is_ascii_lowercase() || b == b'_') {
        return None;
    }
    let kind = match ext {
        "js" => JS,
        "css" => "text/css; charset=utf-8",
        _ => return None,
    };
    let data = WEB.get_file(file)?.contents();
    Some(Reply::bytes(200, kind, data.to_vec()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_pages_files_are_served_and_nothing_else() {
        // Python's test_web_assets_are_served_and_nothing_else.
        let main = web("/web/main.js").unwrap();
        assert_eq!(main.header("content-type"), Some(JS));
        assert!(!main.body_bytes().is_empty());
        assert!(web("/web/editor.css").is_some());
        for bad in [
            "/web/LICENSES.md",
            "/web/../web_editor.py",
            "/web/Main.js",
            "/web/nope.js",
            "/web/.js",
            "/web/main.js.map",
            "/web/sub/x.js",
        ] {
            assert!(web(bad).is_none(), "{bad}");
        }
        assert!(
            String::from_utf8(page().body_bytes())
                .unwrap()
                .contains("<html")
        );
    }
}
