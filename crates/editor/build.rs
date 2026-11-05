// The page's files are embedded (src/assets.rs); rebuild when they change.
fn main() {
    for path in [
        "../../setlist_maker/web",
        "../../setlist_maker/web_editor.html",
        "../../setlist_maker/web_timeline.js",
    ] {
        println!("cargo:rerun-if-changed={path}");
    }
}
