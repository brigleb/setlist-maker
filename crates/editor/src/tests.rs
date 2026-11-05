//! The routes against a set in a temp folder, with a fake Shazam and a fake web. Python's
//! test_web_editor, test_watch and test_uploads cases, plus the app's own routes.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use hindsight_core::artwork::{ArtworkCache, Fetch};
use hindsight_core::model::progress::{Ended, Progress, load_progress, save_progress};
use hindsight_core::model::{Probe, Track, Tracklist, load_tracklist, save_tracklist};
use hindsight_core::run::Listener;
use hindsight_core::shazam::{Heard, NoAnswer};

use crate::{Config, Editor, ListenFor, Reply, Request};

const CLIP: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../core/src/audio/fixtures/stereo48_cbr.mp3"
);

/// What the fake Shazam hears: track A before 300s, B after. Each call waits for a permit, so
/// a test decides how far a run gets; `throttle` calls are answered 429 first.
#[derive(Default)]
struct Gate {
    permits: Mutex<usize>,
    open: Condvar,
    calls: Mutex<Vec<(f64, f64)>>,
    throttle: Mutex<usize>,
}

impl Gate {
    fn allow(&self, n: usize) {
        *self.permits.lock().unwrap() += n;
        self.open.notify_all();
    }
    fn calls(&self) -> Vec<(f64, f64)> {
        self.calls.lock().unwrap().clone()
    }
}

struct FakeShazam(Arc<Gate>);

impl Listener for FakeShazam {
    fn listen(&mut self, t: f64, window: f64) -> Result<Heard, NoAnswer> {
        let gate = &self.0;
        let mut permits = gate.permits.lock().unwrap();
        while *permits == 0 {
            permits = gate.open.wait(permits).unwrap();
        }
        *permits -= 1;
        drop(permits);
        gate.calls.lock().unwrap().push((t, window));
        {
            let mut throttle = gate.throttle.lock().unwrap();
            if *throttle > 0 {
                *throttle -= 1;
                return Err(NoAnswer::Throttled);
            }
        }
        let (artist, title) = if t + window / 2.0 < 300.0 {
            ("A", "Alpha")
        } else {
            ("B", "Beta")
        };
        Ok(Heard {
            result: Some(json!({"artist": artist, "title": title, "confidence": 0.9})),
            offsets: None,
        })
    }
}

/// The web: an image at img.example, nothing anywhere else; every URL asked is noted.
#[derive(Default)]
struct FakeWeb(Mutex<Vec<String>>);

fn png() -> Vec<u8> {
    let img = image::RgbImage::from_pixel(40, 30, image::Rgb([200, 30, 30]));
    let mut out = Vec::new();
    img.write_to(&mut std::io::Cursor::new(&mut out), image::ImageFormat::Png)
        .unwrap();
    out
}

struct WebHandle(Arc<FakeWeb>);

impl Fetch for WebHandle {
    fn get(&self, url: &str) -> Option<Vec<u8>> {
        self.0.0.lock().unwrap().push(url.to_string());
        url.starts_with("https://img.example/").then(png)
    }
    fn final_url(&self, url: &str) -> Option<String> {
        self.0.0.lock().unwrap().push(url.to_string());
        None
    }
}

struct Fixture {
    dir: tempfile::TempDir,
    editor: Arc<Editor>,
    gate: Arc<Gate>,
    web: Arc<FakeWeb>,
    closed: Arc<Mutex<bool>>,
}

const DURATION: f64 = 600.0;

fn fixture() -> Fixture {
    fixture_with(None)
}

fn fixture_with(audio_cap: Option<u64>) -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let gate = Arc::new(Gate::default());
    let web = Arc::new(FakeWeb::default());
    let closed = Arc::new(Mutex::new(false));
    let (g, c) = (gate.clone(), closed.clone());
    let config = Config {
        config_dir: dir.path().join("config"),
        artwork: ArtworkCache::new(
            dir.path().join("cache"),
            Box::new(WebHandle(web.clone())) as Box<dyn Fetch>,
        ),
        peaks_dir: dir.path().join("peaks"),
        listener: Box::new(move |_: &Path, _: ListenFor| {
            Ok((
                Box::new(FakeShazam(g.clone())) as Box<dyn Listener + Send>,
                DURATION,
            ))
        }),
        delay: Duration::ZERO,
        backoff: vec![Duration::ZERO],
        audio_cap,
        find_claude: Box::new(|| None),
        on_close: Box::new(move || *c.lock().unwrap() = true),
    };
    Fixture {
        editor: Editor::new(config),
        dir,
        gate,
        web,
        closed,
    }
}

impl Fixture {
    fn path(&self, name: &str) -> PathBuf {
        self.dir.path().join(name)
    }

    /// `set.mp3` (the fixture clip) and, unless `None`, its tracklist.
    fn set(&self, tracks: Option<Vec<Track>>) -> PathBuf {
        let audio = self.path("set.mp3");
        fs::copy(CLIP, &audio).unwrap();
        if let Some(tracks) = tracks {
            let tl = Tracklist {
                source_file: "set.mp3".into(),
                tracks,
                generated_on: Some("2026-11-05 10:00".into()),
                summary: None,
            };
            save_tracklist(&tl, &self.path("set_tracklist.md"), None).unwrap();
        }
        audio
    }

    fn sample_set(&self) -> PathBuf {
        let mut first = Track::new(0, "Daft Punk", "Around the World");
        first.coverart_url = Some("https://img.example/daft.jpg".into());
        self.set(Some(vec![
            first,
            Track::new(180, "The Chemical Brothers", "Block Rockin' Beats"),
            Track::new(360, "", ""),
            Track::new(540, "Fatboy Slim", "Praise You"),
        ]));
        self.path("set_tracklist.md")
    }

    fn get(&self, target: &str) -> Reply {
        self.editor.handle(&Request::get(target))
    }

    fn post(&self, target: &str, body: Value) -> Reply {
        self.editor
            .handle(&Request::post_json(target, body.to_string()))
    }

    fn open(&self, path: &Path) {
        let r = self.post("/api/open", json!({"path": path}));
        assert_eq!(r.status, 200);
        assert_eq!(r.body_json()["ok"], true, "{}", r.body_json());
    }

    fn tracklist(&self) -> Value {
        self.get("/api/tracklist").body_json()
    }

    /// Lets the run take one call at a time until `job` (a request blocked on the run) ends.
    fn pump<T>(&self, job: std::thread::JoinHandle<T>) -> T {
        while !job.is_finished() {
            self.gate.allow(1);
            std::thread::sleep(Duration::from_millis(5));
        }
        job.join().unwrap()
    }

    fn post_in_background(&self, target: &str, body: Value) -> std::thread::JoinHandle<Reply> {
        let editor = self.editor.clone();
        let req = Request::post_json(target, body.to_string());
        std::thread::spawn(move || editor.handle(&req))
    }

    fn wait_until(&self, what: &str, done: impl Fn(&Fixture) -> bool) {
        let started = Instant::now();
        while !done(self) {
            assert!(
                started.elapsed() < Duration::from_secs(10),
                "timed out waiting for {what}"
            );
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

#[test]
fn nothing_open_is_the_drop_screen() {
    let f = fixture();
    assert_eq!(f.tracklist(), json!({"open": false, "notice": null}));
    assert_eq!(f.get("/api/audio").status, 404);
    assert_eq!(f.post("/api/save", json!({})).status, 409);
    let page = f.get("/");
    assert_eq!(page.status, 200);
    assert!(
        String::from_utf8(page.body_bytes())
            .unwrap()
            .contains("<html")
    );
    assert_eq!(f.get("/timeline.js").status, 200);
    assert_eq!(f.get("/web/main.js").status, 200);
    assert_eq!(f.get("/web/../setlist_maker/web_editor.py").status, 404);
}

#[test]
fn a_dropped_tracklist_opens_its_set_for_editing() {
    let f = fixture();
    let md = f.sample_set();
    f.open(&md);
    let api = f.tracklist();
    assert_eq!(
        (api["open"].clone(), api["live"].clone()),
        (json!(true), json!(false))
    );
    assert_eq!(api["source_file"], "set.mp3");
    assert_eq!(api["tracks"].as_array().unwrap().len(), 4);
    assert_eq!(api["can_embed"], true);
    assert_eq!(api["run"], Value::Null);
    assert_eq!(api["cover"], Value::Null);
    assert!(api["revision"].is_u64());
    // The recording beside it is what plays.
    let audio = f.get("/api/audio");
    assert_eq!(audio.status, 200);
    assert_eq!(audio.body_bytes(), fs::read(CLIP).unwrap());
}

#[test]
fn what_a_drop_opens() {
    use crate::{Opening, classify};
    let f = fixture();
    let audio = f.set(None);
    assert_eq!(classify(&audio), Ok(Opening::Identify(audio.clone())));
    let md = f.sample_set();
    let edit = Opening::Edit {
        tracklist: md.clone(),
        audio: Some(audio.clone()),
    };
    assert_eq!(classify(&audio), Ok(edit));
    assert!(matches!(
        classify(&md),
        Ok(Opening::Edit { audio: Some(_), .. })
    ));
    fs::write(f.path("notes.txt"), "x").unwrap();
    assert!(
        classify(&f.path("notes.txt"))
            .unwrap_err()
            .contains("recording")
    );
    assert!(classify(&f.path("missing.mp3")).is_err());
    let r = f
        .post("/api/open", json!({"path": f.path("notes.txt")}))
        .body_json();
    assert_eq!(r["ok"], false);
}

#[test]
fn a_save_writes_the_files_learns_the_correction_and_bumps_the_revision_on_a_reorder() {
    let f = fixture();
    f.open(&f.sample_set());
    let revision = f.tracklist()["revision"].as_u64().unwrap();
    let r = f.post(
        "/api/save",
        json!({"revision": revision, "summary": "A set.", "tracks": [
            {"index": 0, "artist": "Daft Punk", "title": "Harder Better"},
            {"index": 1, "artist": "The Chemical Brothers", "title": "Block Rockin' Beats", "rejected": true},
        ], "episode": {"title": "Ep 1", "artist": "DJ X"}}),
    );
    assert_eq!(r.status, 200, "{}", r.body_json());
    assert_eq!(
        r.body_json(),
        json!({"ok": true, "rejected": 1, "edited": 1, "revision": revision})
    );
    let saved = load_tracklist(&f.path("set_tracklist.md"))
        .unwrap()
        .tracklist;
    assert_eq!(saved.tracks[0].title, "Harder Better");
    assert_eq!(saved.summary.as_deref(), Some("A set."));
    assert_eq!(saved.tracks.len(), 3, "the rejected row isn't written");
    let corrections = fs::read_to_string(f.path("config/corrections.json")).unwrap();
    assert!(corrections.contains("Harder Better"));
    assert_eq!(
        f.tracklist()["episode"],
        json!({"title": "Ep 1", "artist": "DJ X"})
    );

    // An insert re-sorts, so the revision moves and the old one is refused.
    let r = f.post("/api/save", json!({"revision": revision, "tracks": [{"artist": "New", "title": "Row", "timestamp": 90}]}));
    assert_eq!(r.body_json()["revision"], revision + 1);
    let stale = f.post("/api/save", json!({"revision": revision, "tracks": []}));
    assert_eq!(stale.status, 409);
    // No revision (an older page) isn't checked.
    assert_eq!(f.post("/api/save", json!({"tracks": []})).status, 200);
}

#[test]
fn a_bad_save_is_a_400_and_changes_nothing() {
    let f = fixture();
    f.open(&f.sample_set());
    let before = fs::read(f.path("set_tracklist.md")).unwrap();
    for body in [
        json!([]),
        json!({"tracks": "x"}),
        json!({"tracks": [1]}),
        json!({"episode": "x"}),
        json!({"cover": "upload:0123456789abcdef01234567.jpg"}),
        json!({"cover": "https://x/a.jpg"}),
        json!({"tracks": [{"index": 0, "artist": "x", "title": "y", "coverart_url": "file:///etc/passwd"}]}),
    ] {
        let r = f.post("/api/save", body.clone());
        assert_eq!(r.status, 400, "{body}");
        assert_eq!(r.body_json()["ok"], false);
    }
    let not_json = f.editor.handle(&Request::post_json("/api/save", "{nope"));
    assert_eq!(not_json.status, 400);
    assert_eq!(fs::read(f.path("set_tracklist.md")).unwrap(), before);
    // Not JSON at all, as a cross-site form would send: refused before it's read.
    let form = Request::new("POST", "/api/save").header("content-type", "text/plain");
    assert_eq!(f.editor.handle(&form).status, 415);
}

#[test]
fn audio_ranges_and_the_apps_cap() {
    let f = fixture_with(Some(1000));
    f.open(&f.sample_set());
    let size = fs::metadata(CLIP).unwrap().len();
    let ranged = |h: &str| {
        f.editor
            .handle(&Request::get("/api/audio").header("range", h))
    };
    let r = ranged("bytes=0-1");
    assert_eq!(r.status, 206);
    assert_eq!(
        r.header("content-range"),
        Some(format!("bytes 0-1/{size}").as_str())
    );
    assert_eq!(r.body_bytes(), fs::read(CLIP).unwrap()[..2]);
    assert_eq!(r.header("content-type"), Some("audio/mpeg"));
    // Open-ended, or none at all: capped, and partial so the player asks for the rest.
    let r = ranged("bytes=100-");
    assert_eq!((r.status, r.header("content-length")), (206, Some("1000")));
    assert_eq!(
        r.header("content-range"),
        Some(format!("bytes 100-1099/{size}").as_str())
    );
    assert_eq!(f.get("/api/audio").status, 206);
    assert_eq!(ranged(&format!("bytes={size}-")).status, 416);
    // Uncapped (serve): the whole file, streamed.
    let g = fixture();
    g.open(&g.sample_set());
    let whole = g.get("/api/audio");
    assert_eq!(
        (whole.status, whole.header("content-length")),
        (200, Some(size.to_string().as_str()))
    );
}

#[test]
fn artwork_as_found_and_as_the_embed_writes_it() {
    let f = fixture();
    f.open(&f.sample_set());
    let source = f.get("/api/artwork/source?index=0");
    assert_eq!(source.status, 200);
    assert_eq!(source.header("content-type"), Some("image/png"));
    assert_eq!(source.header("cache-control"), Some("no-store"));
    assert_eq!(source.body_bytes(), png());
    // The artwork panel previews the plain cover the embed writes: square, 600px, JPEG.
    let plain = f.get("/api/artwork?index=0");
    assert_eq!(plain.header("content-type"), Some("image/jpeg"));
    let img = image::load_from_memory(&plain.body_bytes()).unwrap();
    assert_eq!((img.width(), img.height()), (600, 600));
    for bad in ["", "x", "2", "99", "-1"] {
        assert_eq!(
            f.get(&format!("/api/artwork?index={bad}")).status,
            404,
            "{bad}"
        );
        assert_eq!(
            f.get(&format!("/api/artwork/source?index={bad}")).status,
            404,
            "{bad}"
        );
    }
    // No art anywhere: a 404, which the page draws as its gradient.
    assert_eq!(f.get("/api/artwork/source?index=1").status, 404);
    assert_eq!(f.get("/api/artwork?index=1").status, 404);
}

#[test]
fn the_pickers_options_lead_with_the_cover_in_use_and_search_the_names_the_page_sends() {
    let f = fixture();
    f.open(&f.sample_set());
    let r = f
        .get("/api/artwork/options?index=0&artist=Justice&title=Genesis")
        .body_json();
    assert_eq!(
        r["candidates"],
        json!([{"source": "In use", "url": "https://img.example/daft.jpg", "label": "", "current": true}])
    );
    let asked = f.web.0.lock().unwrap().join(" ");
    assert!(asked.contains("Justice"), "{asked}");
    assert!(!asked.contains("Daft"), "searched the saved name: {asked}");
}

#[test]
fn uploads_are_images_only_and_come_back_by_reference() {
    let f = fixture();
    f.open(&f.sample_set());
    let up = |kind: &str, body: Vec<u8>| {
        let mut req = Request::new("POST", "/api/upload").header("content-type", kind);
        req.body = body;
        f.editor.handle(&req)
    };
    assert_eq!(up("multipart/form-data", png()).status, 415);
    assert_eq!(up("text/plain", png()).status, 415);
    assert_eq!(up("image/png", Vec::new()).status, 400);
    assert_eq!(up("image/png", b"not an image".to_vec()).status, 400);
    let big = Request::new("POST", "/api/upload")
        .header("content-type", "image/png")
        .header("content-length", "99999999999");
    assert_eq!(f.editor.handle(&big).status, 413);
    let r = up("image/png", png()).body_json();
    let reference = r["ref"].as_str().unwrap().to_string();
    let served = f.get(&format!("/api/upload/{}", &reference["upload:".len()..]));
    assert_eq!(served.status, 200);
    assert_eq!(
        served.header("cache-control"),
        Some("max-age=31536000, immutable")
    );
    assert_eq!(f.get("/api/upload/../../set.mp3").status, 404);
    // A drop on the window hands the page a path, which it uploads by name.
    fs::write(f.path("cover.png"), png()).unwrap();
    let by_path = f
        .post("/api/upload/path", json!({"path": f.path("cover.png")}))
        .body_json();
    assert_eq!(by_path["ref"], reference.as_str());
    // ...and saving it as the episode cover writes <set>_cover.jpg.
    let r = f.post("/api/save", json!({"cover": reference}));
    assert_eq!(r.status, 200, "{}", r.body_json());
    assert!(f.path("set_cover.jpg").exists());
    assert!(f.tracklist()["cover"].is_u64());
    assert_eq!(f.get("/api/cover").status, 200);
}

fn probe(t: f64, title: &str) -> Probe {
    Probe {
        t,
        window: 30.0,
        purpose: "coverage".into(),
        result: Some(json!({"artist": "X", "title": title, "confidence": 0.9})),
        offsets: None,
    }
}

#[test]
fn the_editor_draws_the_saved_probes_and_samples_into_the_progress_file() {
    let f = fixture();
    let md = f.sample_set();
    let progress = Progress {
        audio_duration: Some(DURATION),
        probes: vec![probe(450.0, "a"), probe(100.0, "b")],
        ..Progress::default()
    };
    save_progress(&f.path("set_progress.json"), &progress).unwrap();
    f.open(&md);
    let p = f.get("/api/probes").body_json();
    assert_eq!(
        (p["live"].clone(), p["can_sample"].clone()),
        (json!(false), json!(true))
    );
    let ts: Vec<f64> = p["probes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x["t"].as_f64().unwrap())
        .collect();
    assert_eq!(ts, [450.0, 100.0]);

    f.gate.allow(1);
    // Centred on the click, but never before the start (the clip is 12s long).
    let r = f.post("/api/sample", json!({"t": 8.0}));
    assert_eq!(r.status, 200, "{}", r.body_json());
    assert_eq!(r.body_json()["probe"]["t"], 0.0);
    assert_eq!(f.gate.calls(), [(0.0, 30.0)]);
    let saved = load_progress(&f.path("set_progress.json")).unwrap();
    assert_eq!(saved.probes.len(), 3);
    assert_eq!(saved.probes[2].purpose, "manual");
    for bad in [json!({"t": -1}), json!({"t": "x"}), json!({})] {
        assert_eq!(f.post("/api/sample", bad).status, 400);
    }
    // Past the end of the recording: nothing to hear.
    assert_eq!(f.post("/api/sample", json!({"t": 13.0})).status, 400);
}

#[test]
fn a_recording_with_nothing_beside_it_is_identified_live_then_edited() {
    let f = fixture();
    let audio = f.set(None);
    f.open(&audio);
    let api = f.tracklist();
    assert_eq!(
        (api["open"].clone(), api["live"].clone()),
        (json!(true), json!(true))
    );
    // Read-only while live: the run writes the tracklist when it finishes.
    assert_eq!(f.post("/api/save", json!({})).status, 409);
    assert_eq!(f.post("/api/chapters", json!({})).status, 409);

    f.gate.allow(3);
    f.wait_until("three probes", |f| {
        f.get("/api/probes").body_json()["probes"]
            .as_array()
            .unwrap()
            .len()
            == 3
    });
    let p = f.get("/api/probes").body_json();
    assert_eq!(
        (p["live"].clone(), p["can_sample"].clone()),
        (json!(true), json!(true))
    );
    assert!(p["next"]["t"].is_f64());
    assert_eq!(p["duration"], DURATION);

    // A click is answered by the run, ahead of its own next probe.
    // (The run may take it at once or after the probe it's on; the answer is the same.)
    let asker = f.post_in_background("/api/sample", json!({"t": 450}));
    let answer = f.pump(asker).body_json();
    assert_eq!(answer["probe"]["t"], 435.0);
    assert_eq!(answer["probe"]["purpose"], "manual");

    // Let it converge: the same window becomes the editor, on the tracklist the run wrote.
    f.gate.allow(10_000);
    f.wait_until("the editor", |f| f.tracklist()["live"] == false);
    let api = f.tracklist();
    let titles: Vec<&str> = api["tracks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["title"].as_str().unwrap())
        .collect();
    assert_eq!(titles, ["Alpha", "Beta"]);
    assert_eq!(
        load_progress(&f.path("set_progress.json")).unwrap().ended,
        Some(Ended::Converged)
    );
    assert!(f.path("set_tracklist.md").exists());
    assert_eq!(
        f.post("/api/save", json!({"revision": api["revision"]}))
            .status,
        200
    );
}

#[test]
fn finish_now_writes_what_was_heard_and_stop_leaves_only_the_progress() {
    let f = fixture();
    let audio = f.set(None);
    f.open(&audio);
    f.gate.allow(2);
    f.wait_until("two probes", |f| f.gate.calls().len() == 2);
    // Stop: back to the drop screen, the tracklist unwritten, the probes kept.
    assert_eq!(f.post("/api/stop", json!({})).status, 200);
    f.gate.allow(1); // the call in flight
    f.wait_until("the drop screen", |f| f.tracklist()["open"] == false);
    assert!(!f.path("set_tracklist.md").exists());
    let kept = load_progress(&f.path("set_progress.json"))
        .unwrap()
        .probes
        .len();
    assert!(kept >= 2);

    // Dropping it again resumes; Finish now writes the tracklist and opens the editor.
    f.open(&audio);
    assert_eq!(f.tracklist()["live"], true);
    f.gate.allow(1);
    assert_eq!(f.post("/api/finish", json!({})).status, 200);
    f.gate.allow(1);
    f.wait_until("the editor", |f| f.tracklist()["live"] == false);
    let api = f.tracklist();
    assert_eq!(api["run"]["ended"], "finished_early");
    assert_eq!(api["run"]["edited"], false);
    // Continue identifying carries on from there.
    assert_eq!(f.post("/api/continue", json!({})).status, 200);
    assert_eq!(f.tracklist()["live"], true);
    f.gate.allow(10_000);
    f.wait_until("the editor again", |f| f.tracklist()["live"] == false);
    assert_eq!(f.tracklist()["run"], Value::Null);
    assert!(
        load_progress(&f.path("set_progress.json"))
            .unwrap()
            .probes
            .len()
            > kept
    );
}

#[test]
fn a_run_shazam_keeps_throttling_pauses_until_resumed() {
    let f = fixture();
    let audio = f.set(None);
    *f.gate.throttle.lock().unwrap() = 2; // the call and its one retry
    f.open(&audio);
    f.gate.allow(2);
    f.wait_until("the pause", |f| {
        !f.get("/api/probes").body_json()["paused"].is_null()
    });
    let p = f.get("/api/probes").body_json();
    assert!(p["paused"].as_str().unwrap().contains("throttling"));
    assert_eq!(p["live"], true);
    assert_eq!(f.post("/api/resume", json!({})).status, 200);
    f.gate.allow(10_000);
    f.wait_until("the editor", |f| f.tracklist()["live"] == false);
    assert_eq!(
        load_progress(&f.path("set_progress.json")).unwrap().ended,
        Some(Ended::Converged)
    );
}

#[test]
fn a_second_drop_while_live_must_say_how_the_run_ends() {
    let f = fixture();
    let audio = f.set(None);
    f.open(&audio);
    let md = f.path("other_tracklist.md");
    save_tracklist(
        &Tracklist {
            source_file: "other.mp3".into(),
            tracks: vec![Track::new(0, "Z", "Zed")],
            ..Tracklist::default()
        },
        &md,
        None,
    )
    .unwrap();
    assert_eq!(f.post("/api/open", json!({"path": md})).status, 409);
    let r = f.pump(f.post_in_background("/api/open", json!({"path": md, "run": "stop"})));
    assert_eq!(r.body_json()["ok"], true);
    assert_eq!(f.tracklist()["source_file"], "other.mp3");
    assert!(!f.path("set_tracklist.md").exists());
}

#[test]
fn closing_the_window_mid_run_says_how_the_run_ends_too() {
    let f = fixture();
    f.open(&f.set(None));
    assert_eq!(f.post("/api/close", json!({})).status, 409);
    assert!(!*f.closed.lock().unwrap());
    f.gate.allow(1);
    f.wait_until("a probe", |f| f.gate.calls().len() == 1);
    let r = f.pump(f.post_in_background("/api/close", json!({"run": "finish"})));
    assert_eq!(r.status, 200);
    assert!(*f.closed.lock().unwrap());
    assert!(
        f.path("set_tracklist.md").exists(),
        "Finish wrote the tracklist before closing"
    );
}

#[test]
fn done_goes_back_to_the_drop_screen() {
    let f = fixture();
    f.open(&f.sample_set());
    let done = f.editor.handle(&Request::new("POST", "/api/done"));
    assert_eq!(done.status, 200);
    assert_eq!(f.tracklist()["open"], false);
}

#[test]
fn a_path_from_the_dock_waits_for_the_page() {
    let f = fixture();
    f.editor.offer(PathBuf::from("/x/set.mp3"));
    assert_eq!(
        f.get("/api/pending").body_json(),
        json!({"path": "/x/set.mp3"})
    );
    assert_eq!(f.get("/api/pending").body_json(), json!({"path": null}));
}

#[test]
fn a_summary_needs_claude() {
    let f = fixture();
    assert_eq!(
        f.get("/api/summary").body_json(),
        json!({"available": false})
    );
    f.open(&f.sample_set());
    assert_eq!(f.post("/api/summary", json!({})).status, 409);
}

#[test]
fn the_peaks_are_the_recordings() {
    let f = fixture();
    f.open(&f.sample_set());
    let r = f.get("/api/peaks");
    assert_eq!(r.status, 200);
    assert_eq!(&r.body_bytes()[..4], b"SMPK");
}
