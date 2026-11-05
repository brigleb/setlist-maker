//! Every route the page calls. Python's `_Handler`: the same paths, statuses and JSON, plus
//! the app's own (open a dropped path, finish or stop a run, resume after throttling, carry on
//! with an unfinished run, a summary, closing the window).

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde_json::{Map, Value, json};

use hindsight_core::artwork::image::chapter_jpeg;
use hindsight_core::artwork::{CHAPTER_IMAGE_SIZE, resize_cover_art_url};
use hindsight_core::audio::peaks::peaks_for;
use hindsight_core::chapters::{self, Episode};
use hindsight_core::model::episode::{clean_tag, episode_tags_in, save_episode_tags_in};
use hindsight_core::model::progress::{append_probe, load_progress};
use hindsight_core::model::uploads::{
    MAX_UPLOAD_BYTES, UploadError, artwork_dir_for, cover_path_for, is_upload_ref,
    set_episode_cover, store_upload, upload_path,
};
use hindsight_core::model::{Track, Tracklist, save_tracklist};
use hindsight_core::run::{self, Sampler};
use hindsight_core::summary;

use crate::api::{self, BadEdit};
use crate::http::{Body, Reply, Request};
use crate::range::byte_range;
use crate::session::{EndRun, ListenFor, Phase, is_mp3_file, unfinished_run};
use crate::{Editor, assets};

/// How long a click on the live timeline waits for the run to get to it. The run answers
/// between its own probes, so this is generous.
const LIVE_SAMPLE_TIMEOUT: Duration = Duration::from_secs(600);

/// A live run would be ended by this, and the page must say how (it asked the user).
const SAY_HOW_THE_RUN_ENDS: &str = "a run is identifying: say whether to finish or stop it";

fn audio_type(path: &Path) -> &'static str {
    let ext = path
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    match ext.as_str() {
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "flac" => "audio/flac",
        "m4a" => "audio/mp4",
        "ogg" => "audio/ogg",
        "aac" => "audio/aac",
        "aiff" => "audio/aiff",
        _ => "application/octet-stream",
    }
}

/// Cover art isn't always JPEG.
fn image_type(data: &[u8]) -> &'static str {
    if data.starts_with(b"\xff\xd8\xff") {
        "image/jpeg"
    } else if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        "image/png"
    } else if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        "image/webp"
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        "image/gif"
    } else {
        "application/octet-stream"
    }
}

fn json_object(body: &[u8]) -> Result<Map<String, Value>, BadEdit> {
    let raw = if body.is_empty() {
        b"{}".as_slice()
    } else {
        body
    };
    match serde_json::from_slice(raw) {
        Ok(Value::Object(o)) => Ok(o),
        Ok(_) => Err(BadEdit("expected a JSON object".into())),
        Err(e) => Err(BadEdit(format!("not JSON: {e}"))),
    }
}

/// What the routes need to know about the set in the window, copied out of the lock.
struct Current {
    tracklist_path: PathBuf,
    audio: Option<PathBuf>,
    tracklist: Tracklist,
}

impl Editor {
    /// Answers one request.
    pub fn handle(self: &Arc<Self>, req: &Request) -> Reply {
        let path = req.path.as_str();
        match req.method.as_str() {
            "GET" | "HEAD" => self.get(req, path),
            "POST" => {
                // A cross-site form or a text/plain fetch can reach `serve`'s port without a
                // preflight; application/json can't be sent cross-origin without one, which
                // nothing here grants. Uploads are image/* for the same reason.
                let raw_upload = path == "/api/upload";
                if !raw_upload && req.content_type() != "application/json" {
                    return Reply::status(415, "expected application/json");
                }
                self.post(req, path)
            }
            _ => Reply::status(405, "method not allowed"),
        }
    }

    fn get(self: &Arc<Self>, req: &Request, path: &str) -> Reply {
        match path {
            "/" => assets::page(),
            "/timeline.js" => assets::timeline(),
            p if p.starts_with("/web/") => {
                assets::web(p).unwrap_or_else(|| Reply::status(404, "not found"))
            }
            "/api/tracklist" => self.tracklist(),
            "/api/probes" => self.probes(),
            "/api/audio" => self.audio(req),
            "/api/peaks" => self.peaks(),
            "/api/artwork" => self.artwork(req, true),
            "/api/artwork/source" => self.artwork(req, false),
            "/api/artwork/options" => self.artwork_options(req),
            p if p.starts_with("/api/upload/") => self.uploaded(&p["/api/upload/".len()..]),
            "/api/cover" => match self.current() {
                Some(c) => image_file(&cover_path_for(&c.tracklist_path), "no-store"),
                None => Reply::status(404, "no set is open"),
            },
            "/api/summary" => Reply::ok(json!({"available": self.claude().is_some()})),
            "/api/pending" => Reply::ok(json!({"path": self.state().pending.take()})),
            _ => Reply::status(404, "not found"),
        }
    }

    fn post(self: &Arc<Self>, req: &Request, path: &str) -> Reply {
        let body = || json_object(&req.body).map_err(|e| Reply::error(400, e));
        let result = match path {
            "/api/save" => return self.save(&req.body),
            "/api/sample" => return self.sample(&req.body),
            "/api/upload" => return self.upload(req),
            "/api/chapters" => return self.embed(),
            // Back to the drop screen; a live run ends as the page says (it asked).
            "/api/done" => body().map(|b| {
                self.close_set(EndRun::parse(b.get("run")).unwrap_or(EndRun::Stop));
                json!({"ok": true})
            }),
            "/api/upload/path" => return self.upload_path(&req.body),
            "/api/open" => body().and_then(|b| self.open_path(&b)),
            "/api/finish" | "/api/stop" | "/api/resume" => self.steer(path),
            "/api/continue" => self.carry_on(),
            "/api/summary" => return self.summarize(),
            "/api/close" => body().and_then(|b| self.close_window(&b)),
            _ => return Reply::status(404, "not found"),
        };
        result.map_or_else(|refused| refused, Reply::ok)
    }

    /// The set in the window, live or being edited.
    fn current(&self) -> Option<Current> {
        let state = self.state();
        match &state.phase {
            Phase::Empty => None,
            Phase::Live(live) => Some(Current {
                tracklist_path: live.paths.tracklist.clone(),
                audio: Some(live.audio.clone()),
                tracklist: live.snapshot().tracklist,
            }),
            Phase::Editing(set) => Some(Current {
                tracklist_path: set.tracklist_path.clone(),
                audio: set.audio.clone(),
                tracklist: set.tracklist.clone(),
            }),
        }
    }

    fn tracklist(&self) -> Reply {
        let state = self.state();
        let (mut api, tracklist_path, audio) = match &state.phase {
            Phase::Empty => {
                return Reply::ok(json!({"open": false, "notice": state.notice}));
            }
            Phase::Live(live) => {
                let mut api = api::tracklist_to_api(&live.snapshot().tracklist);
                api["live"] = json!(true);
                (api, live.paths.tracklist.clone(), Some(live.audio.clone()))
            }
            Phase::Editing(set) => {
                let mut api = api::tracklist_to_api(&set.tracklist);
                api["live"] = json!(false);
                // A run that stopped early can carry on (Continue identifying); `edited` warns
                // that carrying on replaces the edits made since.
                api["run"] = json!(set.unfinished);
                (api, set.tracklist_path.clone(), set.audio.clone())
            }
        };
        api["open"] = json!(true);
        api["revision"] = json!(state.revision);
        drop(state);
        // A version for the page to cache-bust /api/cover with; null with no uploaded cover.
        api["cover"] = std::fs::metadata(cover_path_for(&tracklist_path))
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(Value::Null, |d| json!(d.as_millis() as u64));
        let tags = episode_tags_in(&self.config.config_dir, &tracklist_path);
        api["episode"] = json!({"title": tags.title, "artist": tags.artist});
        api["can_embed"] = json!(audio.as_deref().is_some_and(|a| { is_mp3_file(a) }));
        Reply::ok(api)
    }

    fn probes(&self) -> Reply {
        let state = self.state();
        match &state.phase {
            Phase::Empty => Reply::ok(
                json!({"duration": null, "probes": [], "live": false, "can_sample": false}),
            ),
            Phase::Live(live) => {
                let snap = live.snapshot();
                let paused = live.paused();
                let mut api = api::probes_to_api(&snap.probes, snap.duration);
                api["live"] = json!(true);
                api["next"] = json!(snap.next);
                api["can_sample"] = json!(true);
                // Shazam is still throttling: the run waits for Resume.
                api["paused"] = json!(paused);
                Reply::ok(api)
            }
            Phase::Editing(set) => {
                // Re-read every time: small, and the file this answers about can change.
                let saved = set
                    .progress
                    .exists()
                    .then(|| load_progress(&set.progress).ok())
                    .flatten()
                    .unwrap_or_default();
                let mut api = api::probes_to_api(&saved.probes, saved.audio_duration);
                api["live"] = json!(false);
                api["can_sample"] = json!(set.audio.as_deref().is_some_and(Path::is_file));
                Reply::ok(api)
            }
        }
    }

    fn audio(&self, req: &Request) -> Reply {
        let Some(audio) = self.current().and_then(|c| c.audio).filter(|a| a.is_file()) else {
            return Reply::status(404, "audio not found");
        };
        let Ok(size) = std::fs::metadata(&audio).map(|m| m.len()) else {
            return Reply::status(404, "audio not found");
        };
        let Some((start, len, partial)) =
            byte_range(req.header_value("range"), size, self.config.audio_cap)
        else {
            return Reply::status(416, "range not satisfiable")
                .with("Content-Range", format!("bytes */{size}"));
        };
        let mut reply = Reply {
            status: if partial { 206 } else { 200 },
            headers: vec![
                ("Content-Type", audio_type(&audio).to_string()),
                ("Accept-Ranges", "bytes".to_string()),
                ("Content-Length", len.to_string()),
            ],
            body: Body::File {
                path: audio,
                start,
                len,
            },
        };
        if partial {
            let end = (start + len).saturating_sub(1);
            reply = reply.with("Content-Range", format!("bytes {start}-{end}/{size}"));
        }
        reply
    }

    fn peaks(&self) -> Reply {
        let (audio, duration) = {
            let state = self.state();
            match &state.phase {
                Phase::Live(live) => (Some(live.audio.clone()), live.duration()),
                Phase::Editing(set) => (set.audio.clone(), set.duration()),
                Phase::Empty => (None, None),
            }
        };
        let Some(audio) = audio.filter(|a| a.is_file()) else {
            return Reply::status(404, "audio not found");
        };
        let duration = duration.or_else(|| hindsight_core::audio::undecoded_seconds(&audio).ok());
        match peaks_for(&audio, duration, &self.config.peaks_dir) {
            Ok(data) => Reply::bytes(200, "application/octet-stream", data)
                .with("Cache-Control", "no-store"),
            Err(e) => {
                log::warn!("peaks for {}: {e}", audio.display());
                Reply::status(500, "could not read the audio")
            }
        }
    }

    /// `?index=N` as a track, or the 404 itself: a bad or out-of-range index, and an
    /// unidentified track, which the embed skips too. Shared by the artwork routes, so they
    /// answer for the same tracks.
    fn track_for_query(&self, req: &Request) -> Result<(Track, PathBuf), Reply> {
        let index: usize = req
            .param("index")
            .and_then(|i| i.trim().parse().ok())
            .ok_or_else(|| Reply::status(404, "bad index"))?;
        let current = self
            .current()
            .ok_or_else(|| Reply::status(404, "no set is open"))?;
        let track = current
            .tracklist
            .tracks
            .get(index)
            .ok_or_else(|| Reply::status(404, "no such track"))?;
        if track.is_unidentified() {
            return Err(Reply::status(404, "track is unidentified"));
        }
        Ok((track.clone(), artwork_dir_for(&current.tracklist_path)))
    }

    /// The cover as found (`plain` false), or as the embed will write it: centre-cropped,
    /// resized and compressed (`plain`, the artwork panel's preview). Keyed by saved state,
    /// since that's what the embed reads.
    fn artwork(&self, req: &Request, plain: bool) -> Reply {
        let (track, uploads) = match self.track_for_query(req) {
            Ok(t) => t,
            Err(reply) => return reply,
        };
        let found = self.config.artwork.source_artwork(
            &track.artist,
            &track.title,
            track.coverart_url.as_deref(),
            Some(&uploads),
        );
        let data = if plain {
            found.as_deref().and_then(chapter_jpeg)
        } else {
            found
        };
        match data {
            Some(data) => {
                Reply::bytes(200, image_type(&data), data).with("Cache-Control", "no-store")
            }
            None => Reply::status(404, "no artwork found"),
        }
    }

    /// The covers one track could use. Searches the names the page passes (what the user is
    /// looking at, maybe a correction not yet saved), falling back to the saved ones; the one
    /// in use comes first, at the size the embed would fetch it.
    fn artwork_options(&self, req: &Request) -> Reply {
        let (track, _) = match self.track_for_query(req) {
            Ok(t) => t,
            Err(reply) => return reply,
        };
        let pick = |k: &str, saved: &str| {
            req.param(k)
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| saved.trim().to_string())
        };
        let (artist, title) = (pick("artist", &track.artist), pick("title", &track.title));
        let in_use = track
            .coverart_url
            .as_deref()
            .map(|u| resize_cover_art_url(u, CHAPTER_IMAGE_SIZE));
        let mut candidates = Vec::new();
        if let Some(url) = &in_use {
            let source = if is_upload_ref(Some(url)) {
                "Uploaded"
            } else {
                "In use"
            };
            candidates.push((source.to_string(), url.clone(), String::new()));
        }
        for c in self.config.artwork.options(&artist, &title) {
            candidates.push((c.source, c.url, c.label));
        }
        let mut seen = std::collections::HashSet::new();
        let unique: Vec<Value> = candidates
            .into_iter()
            .filter(|(_, url, _)| seen.insert(url.clone()))
            .map(|(source, url, label)| {
                let current = Some(&url) == in_use.as_ref();
                json!({"source": source, "url": url, "label": label, "current": current})
            })
            .collect();
        Reply::ok(json!({"candidates": unique, "error": null}))
    }

    fn uploaded(&self, name: &str) -> Reply {
        let Some(current) = self.current() else {
            return Reply::status(404, "no set is open");
        };
        let dir = artwork_dir_for(&current.tracklist_path);
        match upload_path(&dir, &format!("upload:{name}")) {
            Some(path) => image_file(&path, "max-age=31536000, immutable"),
            None => Reply::status(404, "not found"),
        }
    }

    fn save(&self, body: &[u8]) -> Reply {
        let _writing = self.writing.lock().unwrap_or_else(|e| e.into_inner());
        let mut state = self.state();
        let revision = state.revision;
        let set = match &mut state.phase {
            Phase::Editing(set) => set,
            // The run writes the tracklist when it finishes, over whatever is there.
            Phase::Live(_) => {
                return Reply::error(409, "still identifying; edits open when the run finishes");
            }
            Phase::Empty => return Reply::error(409, "no set is open"),
        };
        let data = match json_object(body) {
            Ok(d) => d,
            Err(e) => return Reply::error(400, e),
        };
        if let Some(sent) = data.get("revision").filter(|v| !v.is_null())
            && sent.as_u64() != Some(revision)
        {
            return Reply::error(
                409,
                "the tracklist changed since this page loaded; reload it",
            );
        }
        let checked = (|| -> Result<_, BadEdit> {
            let edits: Vec<Map<String, Value>> = match data.get("tracks") {
                None => Vec::new(),
                Some(Value::Array(rows)) => rows
                    .iter()
                    .map(|r| r.as_object().cloned())
                    .collect::<Option<_>>()
                    .ok_or_else(|| BadEdit("tracks must be a list of objects".into()))?,
                Some(_) => return Err(BadEdit("tracks must be a list of objects".into())),
            };
            let episode = match data.get("episode") {
                None => None,
                Some(Value::Object(e)) => Some(e.clone()),
                Some(_) => {
                    return Err(BadEdit(
                        "episode must be an object with a title and an artist".into(),
                    ));
                }
            };
            // Absent: unchanged. Null: removed. Else an uploaded image, checked before
            // anything is applied, so a bad cover saves nothing.
            let cover = match data.get("cover") {
                None => None,
                Some(Value::Null) => Some(None),
                Some(Value::String(r)) if is_upload_ref(Some(r)) => {
                    let stored = upload_path(&artwork_dir_for(&set.tracklist_path), r);
                    if !stored.is_some_and(|p| p.exists()) {
                        return Err(BadEdit(format!("uploaded image {r} is missing")));
                    }
                    Some(Some(r.clone()))
                }
                Some(other) => {
                    return Err(BadEdit(format!(
                        "episode cover must be an uploaded image -- got {other}"
                    )));
                }
            };
            Ok((edits, episode, cover))
        })();
        let (edits, episode, cover) = match checked {
            Ok(c) => c,
            Err(e) => return Reply::error(400, e),
        };
        let reordered = match api::apply_edits(
            &mut set.tracklist,
            &edits,
            Some(&mut set.corrections),
            data.get("summary"),
        ) {
            Ok(r) => r,
            Err(e) => return Reply::error(400, e),
        };
        if let Some(Some(_)) = &cover {
            // One episode cover: an uploaded one replaces the starred track.
            for t in &mut set.tracklist.tracks {
                t.is_episode_cover = false;
            }
        }
        let written = (|| -> Result<(), String> {
            save_tracklist(&set.tracklist, &set.tracklist_path, Some(&set.corrections))
                .map_err(|e| e.to_string())?;
            if let Some(cover) = &cover {
                set_episode_cover(&set.tracklist_path, cover.as_deref())
                    .map_err(|e| e.to_string())?;
            }
            if let Some(e) = &episode {
                save_episode_tags_in(
                    &self.config.config_dir,
                    &set.tracklist_path,
                    &clean_tag(e.get("title")),
                    &clean_tag(e.get("artist")),
                )
                .map_err(|e| e.to_string())?;
            }
            Ok(())
        })();
        // The edits are in memory now, whether or not the write landed.
        let (rejected, edited) = (
            set.tracklist.tracks.iter().filter(|t| t.rejected).count(),
            set.tracklist
                .tracks
                .iter()
                .filter(|t| t.was_corrected())
                .count(),
        );
        if set.unfinished.is_some() {
            set.unfinished = unfinished_run(set.audio.as_deref());
        }
        if reordered {
            state.revision += 1;
        }
        if let Err(why) = written {
            return Reply::error(500, why);
        }
        Reply::ok(
            json!({"ok": true, "rejected": rejected, "edited": edited, "revision": state.revision}),
        )
    }

    fn sample(&self, body: &[u8]) -> Reply {
        let t = json_object(body)
            .ok()
            .and_then(|b| b.get("t").and_then(Value::as_f64))
            .filter(|t| t.is_finite() && *t >= 0.0);
        let Some(t) = t else {
            return Reply::error(400, "t must be seconds");
        };
        enum Ask {
            Live(Arc<run::Questions>, Option<f64>),
            Edit {
                audio: PathBuf,
                progress: PathBuf,
                duration: Option<f64>,
                sampler: crate::session::SamplerSlot,
            },
        }
        let ask = {
            let state = self.state();
            match &state.phase {
                Phase::Live(live) => Ask::Live(live.questions.clone(), live.duration()),
                Phase::Editing(set) if set.audio.as_deref().is_some_and(Path::is_file) => {
                    Ask::Edit {
                        audio: set.audio.clone().expect("checked"),
                        progress: set.progress.clone(),
                        duration: set.duration(),
                        sampler: set.sampler.clone(),
                    }
                }
                _ => return Reply::error(409, "sampling is unavailable"),
            }
        };
        let end = match &ask {
            Ask::Live(_, d) => *d,
            Ask::Edit { duration, .. } => *duration,
        };
        if let Some(end) = end
            && t > end
        {
            // Past the end there's nothing to hear, and the probe would be kept forever.
            return Reply::error(400, format!("t is past the end of the audio ({end:.0}s)"));
        }
        let probe = match ask {
            Ask::Live(questions, _) => match questions.ask(t, LIVE_SAMPLE_TIMEOUT) {
                Ok(p) => p,
                Err(why) => return Reply::error(409, why),
            },
            Ask::Edit {
                audio,
                progress,
                duration,
                sampler,
            } => {
                // Spaced from the window's last Shazam call, whichever set made it.
                let sampler = sampler.get_or_init(|| {
                    (self.config.listener)(&audio, ListenFor::Sample)
                        .map(|(l, _)| Sampler::new(l, self.config.delay, *self.last_shazam()))
                });
                let probe = match sampler {
                    Ok(s) => s.sample(t).map_err(|e| e.to_string()),
                    Err(e) => Err(e.clone()),
                };
                *self.last_shazam() = Some(std::time::Instant::now());
                let probe = match probe {
                    Ok(p) => p,
                    Err(why) => return Reply::error(502, why),
                };
                if let Err(e) = append_probe(&progress, probe.clone(), duration) {
                    return Reply::error(502, format!("can't save the sample: {e}"));
                }
                probe
            }
        };
        Reply::ok(json!({"ok": true, "probe": api::probe_to_api(&probe)}))
    }

    /// Stores an uploaded image beside the tracklist and answers with its reference. Storing
    /// isn't choosing: the page pins the reference with an ordinary edit.
    fn upload(&self, req: &Request) -> Reply {
        if !req.content_type().starts_with("image/") {
            return Reply::status(415, "expected an image/* body");
        }
        let declared: usize = req
            .header_value("content-length")
            .and_then(|l| l.trim().parse().ok())
            .unwrap_or(req.body.len());
        self.store(declared.max(req.body.len()), &req.body)
    }

    /// An image dropped on the window over a drop zone: the app has its path, the page doesn't.
    fn upload_path(&self, body: &[u8]) -> Reply {
        let path = match json_object(body)
            .map(|b| b.get("path").and_then(Value::as_str).map(PathBuf::from))
        {
            Ok(Some(p)) => p,
            _ => return Reply::error(400, "path must be a file's path"),
        };
        let size = std::fs::metadata(&path)
            .map(|m| m.len() as usize)
            .unwrap_or(0);
        if size > MAX_UPLOAD_BYTES {
            return self.store(size, &[]);
        }
        match std::fs::read(&path) {
            Ok(data) => self.store(data.len(), &data),
            Err(e) => Reply::error(400, format!("can't read {}: {e}", path.display())),
        }
    }

    fn store(&self, size: usize, data: &[u8]) -> Reply {
        if size > MAX_UPLOAD_BYTES {
            let mb = MAX_UPLOAD_BYTES / (1024 * 1024);
            return Reply::error(413, format!("image is over {mb} MB"));
        }
        if data.is_empty() {
            return Reply::error(400, "empty upload");
        }
        let Some(current) = self.current() else {
            return Reply::error(409, "no set is open");
        };
        match store_upload(&artwork_dir_for(&current.tracklist_path), data) {
            Ok(reference) => Reply::ok(json!({"ok": true, "ref": reference})),
            Err(UploadError::Io(e)) => Reply::error(500, e),
            Err(e) => Reply::error(400, e),
        }
    }

    /// Embeds chapter markers, their pictures and the episode cover into the MP3, from the
    /// saved tracklist (the page saves first), as `hindsight chapters` does. One at a time.
    fn embed(&self) -> Reply {
        let (tracklist, tracklist_path, audio) = {
            let state = self.state();
            match &state.phase {
                Phase::Live(_) => return Reply::error(409, "still identifying"),
                Phase::Empty => return Reply::error(409, "no set is open"),
                Phase::Editing(set) => (
                    set.tracklist.clone(),
                    set.tracklist_path.clone(),
                    set.audio.clone(),
                ),
            }
        };
        let Some(audio) = audio.filter(|a| is_mp3_file(a)) else {
            return Reply::error(400, "chapter markers need the set's MP3");
        };
        if !tracklist
            .tracks
            .iter()
            .any(|t| !t.rejected && !t.is_unidentified())
        {
            return Reply::error(400, "no identified tracks to mark");
        }
        let Ok(_writing) = self.writing.try_lock() else {
            return Reply::error(409, "already embedding");
        };
        let seconds = match hindsight_core::audio::undecoded_seconds(&audio) {
            Ok(s) => s,
            Err(e) => return Reply::error(500, e),
        };
        let tags = episode_tags_in(&self.config.config_dir, &tracklist_path);
        let prepared = chapters::prepare(
            &tracklist,
            &tracklist_path,
            (seconds * 1000.0) as u32,
            &tags,
            None,
            true,
            &self.config.artwork,
            &mut |_| {},
        );
        let episode: &Episode = &prepared.episode;
        match chapters::embed(&audio, episode, &prepared.chapters) {
            Ok(_) => Reply::ok(json!({
                "ok": true,
                "chapters": prepared.chapters.len(),
                "images": prepared.images(),
                "cover": prepared.cover_from.is_some(),
            })),
            Err(e) => Reply::error(500, format!("{}: {e}", audio.display())),
        }
    }

    /// Opens a dropped path. While a run is live the page must say how it ends (it asked).
    fn open_path(self: &Arc<Self>, body: &Map<String, Value>) -> Result<Value, Reply> {
        let path = body
            .get("path")
            .and_then(Value::as_str)
            .map(PathBuf::from)
            .ok_or_else(|| Reply::error(409, "path must be a file's path"))?;
        let end = EndRun::parse(body.get("run"));
        if self.is_live() && end.is_none() {
            return Err(Reply::error(409, SAY_HOW_THE_RUN_ENDS));
        }
        match self.open(&path, end.unwrap_or(EndRun::Finish)) {
            Ok(()) => Ok(json!({"ok": true})),
            Err(why) => Ok(json!({"ok": false, "error": why})),
        }
    }

    /// Finish now, Stop, or Resume after throttling. None of them waits: the page sees the
    /// run end on its next poll.
    fn steer(&self, path: &str) -> Result<Value, Reply> {
        let state = self.state();
        let Phase::Live(live) = &state.phase else {
            return Err(Reply::error(409, "nothing is identifying"));
        };
        match path {
            "/api/stop" => {
                live.abort.store(true, std::sync::atomic::Ordering::SeqCst);
                live.stop.request();
            }
            "/api/finish" => live.stop.request(),
            // Only while paused: a stray Resume would otherwise cut the next pause short.
            _ if live.paused().is_some() => live.stop.resume(),
            _ => return Err(Reply::error(409, "the run isn't paused")),
        }
        Ok(json!({"ok": true}))
    }

    /// Continue identifying a run that stopped early. The page warns first when the tracklist
    /// was edited since: the run's finish writes over it.
    fn carry_on(self: &Arc<Self>) -> Result<Value, Reply> {
        let audio = {
            let state = self.state();
            match &state.phase {
                Phase::Editing(set) if set.unfinished.is_some() => set.audio.clone(),
                _ => None,
            }
        };
        let audio = audio.ok_or_else(|| Reply::error(409, "no unfinished run to carry on"))?;
        self.start_live(&audio);
        Ok(json!({"ok": true}))
    }

    /// A one-paragraph description of the saved tracklist, from the `claude` CLI. The page
    /// puts it in the description field, unsaved.
    fn summarize(&self) -> Reply {
        let Some(claude) = self.claude().map(Path::to_path_buf) else {
            return Reply::error(409, "the claude CLI isn't installed");
        };
        let lines: Vec<String> = match self.current() {
            Some(c) => c
                .tracklist
                .tracks
                .iter()
                .filter(|t| !t.rejected && !t.is_unidentified())
                .map(|t| format!("{} - {}", t.artist, t.title))
                .collect(),
            None => return Reply::error(409, "no set is open"),
        };
        match summary::generate(&claude, &lines) {
            Ok(text) => Reply::ok(json!({"ok": true, "summary": text})),
            Err(why) => Reply::error(502, why),
        }
    }

    /// Closes the window, ending a live run as the page says (it asked).
    fn close_window(&self, body: &Map<String, Value>) -> Result<Value, Reply> {
        if self.is_live() {
            let end = EndRun::parse(body.get("run"))
                .ok_or_else(|| Reply::error(409, SAY_HOW_THE_RUN_ENDS))?;
            self.end_live(end);
        }
        (self.config.on_close)();
        Ok(json!({"ok": true}))
    }
}

fn image_file(path: &Path, cache: &str) -> Reply {
    match std::fs::read(path) {
        Ok(data) => Reply::bytes(200, "image/jpeg", data).with("Cache-Control", cache.to_string()),
        Err(_) => Reply::status(404, "not found"),
    }
}
