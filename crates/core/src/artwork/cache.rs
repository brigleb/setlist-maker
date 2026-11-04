//! A track's cover art on disk, so a lookup happens once per track ever.
//!
//! Both answers are kept: the bytes in `<key>.src`, or an empty `<key>.fallback` marker when
//! the lookup found nothing, which stops a track with no findable art from re-running the
//! six-request waterfall on every embed, in every process. The picker's alternates are
//! `<key>.cands`. Keys are Python's (`artwork_cache.cache_key`) to the byte, so the cache
//! copied from `setlist-maker` on first launch is a warm one. Python's `.jpg` composites
//! aren't read: the chapter picture is made from the source now.
//!
//! An `upload:` reference is read from the set's `_artwork/` folder and touches no cache: a
//! missing file (iCloud not synced yet) must not leave a marker that would keep the track
//! artless once it arrives.
//!
//! Concurrency, for the editor's threads: one lock per key, so two requests for a track look
//! it up once, and at most [`MAX_CONCURRENT_FETCHES`] lookups at a time. Cache hits take
//! neither.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, LazyLock, Mutex};

use super::{ArtworkCandidate, CHAPTER_IMAGE_SIZE, Fetch, sources};
use crate::model::fileio::{atomic_write, cache_dir};
use crate::model::uploads::{is_upload_ref, read_upload};

/// Lookups in flight at once. Each miss can be six requests, so an editor scrolling 60 rows
/// mustn't start 60 of them.
pub const MAX_CONCURRENT_FETCHES: usize = 4;

/// `~/.cache/hindsight/artwork` (or under `$XDG_CACHE_HOME`).
pub fn default_dir() -> PathBuf {
    cache_dir().join("artwork")
}

/// Python's key: sha256 of artist, title, URL (empty for none) and size, NUL-separated. A NUL
/// can't appear in the text, so the fields can't run together, and an edit to any of them is
/// a new key: there is no invalidation to forget.
pub fn cache_key(artist: &str, title: &str, coverart_url: Option<&str>, size: u32) -> String {
    let raw = [artist, title, coverart_url.unwrap_or(""), &size.to_string()].join("\0");
    crate::sha256_hex(raw.as_bytes())
}

static KEY_LOCKS: LazyLock<Mutex<HashMap<String, Arc<Mutex<()>>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn lock_for(key: &str) -> Arc<Mutex<()>> {
    let mut locks = KEY_LOCKS.lock().unwrap_or_else(|e| e.into_inner());
    locks.entry(key.to_string()).or_default().clone()
}

/// A counting semaphore: [`MAX_CONCURRENT_FETCHES`] slots.
struct Slots {
    free: Mutex<usize>,
    freed: Condvar,
}

static SLOTS: Slots = Slots {
    free: Mutex::new(MAX_CONCURRENT_FETCHES),
    freed: Condvar::new(),
};

struct Slot;

impl Slots {
    fn take(&'static self) -> Slot {
        let mut free = self.free.lock().unwrap_or_else(|e| e.into_inner());
        while *free == 0 {
            free = self.freed.wait(free).unwrap_or_else(|e| e.into_inner());
        }
        *free -= 1;
        Slot
    }
}

impl Drop for Slot {
    fn drop(&mut self) {
        *SLOTS.free.lock().unwrap_or_else(|e| e.into_inner()) += 1;
        SLOTS.freed.notify_one();
    }
}

/// The cache in one folder, looking things up through one [`Fetch`].
pub struct ArtworkCache<F: Fetch> {
    dir: PathBuf,
    fetch: F,
}

impl<F: Fetch> ArtworkCache<F> {
    pub fn new(dir: impl Into<PathBuf>, fetch: F) -> Self {
        ArtworkCache {
            dir: dir.into(),
            fetch,
        }
    }

    fn path(&self, key: &str, ext: &str) -> PathBuf {
        self.dir.join(format!("{key}.{ext}"))
    }

    /// The cached bytes; an empty file (an interrupted write) is a miss. No format check:
    /// sources hand back PNG and WebP as well as JPEG.
    fn cached_source(&self, key: &str) -> Option<Vec<u8>> {
        fs::read(self.path(key, "src"))
            .ok()
            .filter(|d| !d.is_empty())
    }

    /// An unreadable cache folder is "don't know", so the lookup runs.
    fn known_artless(&self, key: &str) -> bool {
        self.path(key, "fallback").try_exists().unwrap_or(false)
    }

    /// Best effort: an unwritable cache only means looking again next time.
    fn store(&self, key: &str, ext: &str, data: &[u8]) {
        if let Err(e) = atomic_write(&self.path(key, ext), data) {
            log::debug!("artwork cache write skipped for {key}.{ext}: {e}");
        }
    }

    /// The cover art for a track as found, or `None` when no source has any (or an uploaded
    /// file is missing). Cached bytes win over a marker if both somehow exist.
    pub fn source_artwork(
        &self,
        artist: &str,
        title: &str,
        coverart_url: Option<&str>,
        uploads_dir: Option<&Path>,
    ) -> Option<Vec<u8>> {
        if let Some(reference) = coverart_url.filter(|u| is_upload_ref(Some(u))) {
            let data = read_upload(uploads_dir, reference);
            if data.is_none() {
                log::warn!("uploaded artwork {reference} for '{artist} - {title}' is missing");
            }
            return data;
        }
        let key = cache_key(artist, title, coverart_url, CHAPTER_IMAGE_SIZE);
        let hit = |c: &Self| -> Option<Option<Vec<u8>>> {
            if let Some(data) = c.cached_source(&key) {
                return Some(Some(data));
            }
            c.known_artless(&key).then_some(None)
        };
        if let Some(answer) = hit(self) {
            return answer;
        }
        let lock = lock_for(&key);
        let _held = lock.lock().unwrap_or_else(|e| e.into_inner());
        // Another thread may have answered while this one waited.
        if let Some(answer) = hit(self) {
            return answer;
        }
        let found = {
            let _slot = SLOTS.take();
            sources::fetch_artwork(&self.fetch, artist, title, coverart_url, CHAPTER_IMAGE_SIZE)
        };
        match &found {
            Some(data) => self.store(&key, "src", data),
            None => self.store(&key, "fallback", b""),
        }
        found
    }

    /// The picker's alternates for a track, cached. Keyed without a URL: what a search turns
    /// up depends on the names alone. An empty answer isn't cached; it more often means the
    /// network was down than that there's nothing, and retrying costs the user one click.
    pub fn options(&self, artist: &str, title: &str) -> Vec<ArtworkCandidate> {
        let key = cache_key(artist, title, None, CHAPTER_IMAGE_SIZE);
        let cached = |c: &Self| -> Option<Vec<ArtworkCandidate>> {
            serde_json::from_slice(&fs::read(c.path(&key, "cands")).ok()?).ok()
        };
        if let Some(found) = cached(self) {
            return found;
        }
        // Key lock, then a slot: the order `source_artwork` takes, so the two can't deadlock.
        let lock = lock_for(&key);
        let _held = lock.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(found) = cached(self) {
            return found;
        }
        let found = {
            let _slot = SLOTS.take();
            sources::candidates(&self.fetch, artist, title, CHAPTER_IMAGE_SIZE)
        };
        if !found.is_empty()
            && let Ok(json) = serde_json::to_vec(&found)
        {
            self.store(&key, "cands", &json);
        }
        found
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    use super::super::fetch::fake::Canned;
    use super::*;

    #[test]
    fn keys_match_pythons() {
        // uv run python -c "from setlist_maker.artwork_cache import cache_key;
        //   print(cache_key('Daft Punk','Around the World','http://x/y.jpg',600))" (and so on)
        assert_eq!(
            cache_key("Daft Punk", "Around the World", Some("http://x/y.jpg"), 600),
            "95982f01e8104fcc247a9f23c908b751cc45720fc030ee595614a5ae10f7d859"
        );
        assert_eq!(
            cache_key("Altın Gün", "Kesik Çayır", None, 600),
            "8e0403ab9119afe497ad9a1080e6e6aab2eecb2ee7babcab9726f0ecf561ba9e"
        );
        assert_eq!(
            cache_key("", "", None, 600),
            "4ba0fb260e584c48cbcf446f8f892346da3a83960d0bbb031c8d7c4daf710de1"
        );
        assert_ne!(
            cache_key("ab", "c", None, 600),
            cache_key("a", "bc", None, 600)
        );
    }

    /// A fake whose iTunes search finds one cover for "A - T".
    fn one_cover() -> Canned {
        Canned::default()
            .body(
                "https://itunes.apple.com/search?term=A+T&entity=song&limit=1",
                r#"{"results": [{"artworkUrl100": "https://i/100x100bb.jpg"}]}"#,
            )
            .body("https://i/600x600bb.jpg", "cover")
    }

    #[test]
    fn a_found_cover_is_cached_and_the_next_ask_is_free() {
        let dir = tempfile::tempdir().unwrap();
        let cache = ArtworkCache::new(dir.path(), one_cover());
        assert_eq!(
            cache.source_artwork("A", "T", None, None).as_deref(),
            Some(&b"cover"[..])
        );
        let key = cache_key("A", "T", None, 600);
        assert_eq!(
            fs::read(dir.path().join(format!("{key}.src"))).unwrap(),
            b"cover"
        );
        let asked = cache.fetch.asked().len();
        assert_eq!(
            cache.source_artwork("A", "T", None, None).as_deref(),
            Some(&b"cover"[..])
        );
        assert_eq!(cache.fetch.asked().len(), asked);
    }

    #[test]
    fn nothing_found_is_remembered_across_processes() {
        let dir = tempfile::tempdir().unwrap();
        let cache = ArtworkCache::new(dir.path(), Canned::default());
        assert_eq!(cache.source_artwork("A", "T", None, None), None);
        let key = cache_key("A", "T", None, 600);
        assert!(dir.path().join(format!("{key}.fallback")).exists());
        // A new cache over the same folder (a later run) asks nothing.
        let later = ArtworkCache::new(dir.path(), Canned::default());
        assert_eq!(later.source_artwork("A", "T", None, None), None);
        assert!(later.fetch.asked().is_empty());
    }

    #[test]
    fn cached_bytes_win_over_a_stale_marker_and_an_empty_file_is_a_miss() {
        let dir = tempfile::tempdir().unwrap();
        let key = cache_key("A", "T", None, 600);
        fs::write(dir.path().join(format!("{key}.fallback")), "").unwrap();
        fs::write(dir.path().join(format!("{key}.src")), "art").unwrap();
        let cache = ArtworkCache::new(dir.path(), Canned::default());
        assert_eq!(
            cache.source_artwork("A", "T", None, None).as_deref(),
            Some(&b"art"[..])
        );

        fs::write(dir.path().join(format!("{key}.src")), "").unwrap();
        fs::remove_file(dir.path().join(format!("{key}.fallback"))).unwrap();
        let cache = ArtworkCache::new(dir.path(), one_cover());
        assert_eq!(
            cache.source_artwork("A", "T", None, None).as_deref(),
            Some(&b"cover"[..])
        );
    }

    #[test]
    fn an_edited_name_is_a_new_lookup() {
        let dir = tempfile::tempdir().unwrap();
        let cache = ArtworkCache::new(dir.path(), one_cover());
        cache.source_artwork("A", "T", None, None);
        let before = cache.fetch.asked().len();
        cache.source_artwork("A", "T2", None, None);
        assert!(cache.fetch.asked().len() > before);
    }

    #[test]
    fn an_upload_is_read_from_the_set_and_a_missing_one_marks_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let uploads = dir.path().join("set_artwork");
        fs::create_dir(&uploads).unwrap();
        let name = "0123456789abcdef01234567";
        let reference = format!("upload:{name}.jpg");
        let cache = ArtworkCache::new(dir.path().join("cache"), Canned::default());

        assert_eq!(
            cache.source_artwork("A", "T", Some(&reference), Some(&uploads)),
            None
        );
        assert!(
            !dir.path().join("cache").exists(),
            "no marker, no cache at all"
        );
        assert!(cache.fetch.asked().is_empty());

        fs::write(uploads.join(format!("{name}.jpg")), "mine").unwrap();
        assert_eq!(
            cache
                .source_artwork("A", "T", Some(&reference), Some(&uploads))
                .as_deref(),
            Some(&b"mine"[..])
        );
    }

    #[test]
    fn an_unwritable_cache_still_answers() {
        let dir = tempfile::tempdir().unwrap();
        let blocked = dir.path().join("file");
        fs::write(&blocked, "").unwrap();
        let cache = ArtworkCache::new(blocked.join("artwork"), one_cover());
        assert_eq!(
            cache.source_artwork("A", "T", None, None).as_deref(),
            Some(&b"cover"[..])
        );
    }

    /// A fetch that counts lookups and is slow enough for threads to pile up behind it.
    struct Slow {
        inner: Canned,
        calls: AtomicUsize,
        busy: AtomicUsize,
        most: AtomicUsize,
    }

    impl Fetch for Slow {
        fn get(&self, url: &str) -> Option<Vec<u8>> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let now = self.busy.fetch_add(1, Ordering::SeqCst) + 1;
            self.most.fetch_max(now, Ordering::SeqCst);
            std::thread::sleep(Duration::from_millis(20));
            self.busy.fetch_sub(1, Ordering::SeqCst);
            self.inner.get(url)
        }
        fn final_url(&self, url: &str) -> Option<String> {
            self.inner.final_url(url)
        }
    }

    #[test]
    fn concurrent_asks_look_up_once_per_track_and_four_at_a_time() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Arc::new(ArtworkCache::new(
            dir.path(),
            Slow {
                inner: Canned::default(),
                calls: AtomicUsize::new(0),
                busy: AtomicUsize::new(0),
                most: AtomicUsize::new(0),
            },
        ));
        let threads: Vec<_> = (0..24)
            .map(|i| {
                let cache = cache.clone();
                // Three threads per track, eight tracks; a unique artist keeps other tests'
                // keys (the locks are process-wide) out of it.
                std::thread::spawn(move || {
                    cache.source_artwork(&format!("Concurrent {}", i % 8), "T", None, None)
                })
            })
            .collect();
        for t in threads {
            assert_eq!(t.join().unwrap(), None);
        }
        // A lookup that finds nothing is 3 requests (iTunes, Deezer's first query, MusicBrainz):
        // 8 tracks, looked up once each.
        assert_eq!(cache.fetch.calls.load(Ordering::SeqCst), 8 * 3);
        assert!(cache.fetch.most.load(Ordering::SeqCst) <= MAX_CONCURRENT_FETCHES);
    }

    #[test]
    fn options_are_cached_unless_empty_and_ignore_the_saved_url() {
        let dir = tempfile::tempdir().unwrap();
        let body = r#"{"results": [{"artworkUrl100": "https://i/100x100bb.jpg", "collectionName": "LP"}]}"#;
        let cache = ArtworkCache::new(
            dir.path(),
            Canned::default().body(
                "https://itunes.apple.com/search?term=A+T&entity=song&limit=3",
                body,
            ),
        );
        let want = [ArtworkCandidate::new(
            "iTunes",
            "https://i/600x600bb.jpg",
            "LP",
        )];
        assert_eq!(cache.options("A", "T"), want);
        let key = cache_key("A", "T", None, 600);
        let file = dir.path().join(format!("{key}.cands"));
        let asked = cache.fetch.asked().len();
        assert_eq!(cache.options("A", "T"), want);
        assert_eq!(cache.fetch.asked().len(), asked);

        // A file it can't parse is looked up again.
        fs::write(&file, "{not json").unwrap();
        assert_eq!(cache.options("A", "T"), want);
        // Python's own file, with its spacing, reads.
        fs::write(&file, r#"[{"source": "iTunes", "url": "u", "label": ""}]"#).unwrap();
        assert_eq!(
            cache.options("A", "T"),
            [ArtworkCandidate::new("iTunes", "u", "")]
        );

        let empty = ArtworkCache::new(dir.path(), Canned::default());
        assert!(empty.options("B", "T").is_empty());
        assert!(
            !dir.path()
                .join(format!("{}.cands", cache_key("B", "T", None, 600)))
                .exists()
        );
    }
}
