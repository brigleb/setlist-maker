//! Waveform peaks for the editor's timeline and scrubber (#57), in Python's `SMPK` format.
//!
//! The file is a 24-byte little-endian header, `"SMPK"`, version, rate, pair count, duration
//! (`<4sIIId`), then one int8 (min, max) pair per 1/50s: the high byte of the 16-bit sample,
//! as Python's `(v >> 8) & 0xFF`. 1.4MB for four hours, finer than the timeline's deepest zoom.
//!
//! Python decoded to 8kHz mono with ffmpeg and took each bin's min and max. Here the mono mix
//! is the mean of the channels at the file's own rate, so a peak can be a little sharper than
//! ffmpeg's resampled one; the page draws either the same way. Decoding is the slow part, so a
//! file whose length is known is cut into [`SEGMENTS`] stretches of whole seconds decoded in
//! parallel, each padded or trimmed to exactly its bins so one decoding a few samples long
//! can't shift the rest.
//!
//! Cached under `~/.cache/hindsight/peaks`, keyed as Python keyed it (the resolved path, mtime
//! and size), so a copied cache hits and an edited file misses once.

use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex};

use super::{AudioError, Decoder};
use crate::model::fileio::{atomic_write, cache_dir};

/// Min/max pairs per second.
pub const PEAK_RATE: u32 = 50;
pub const FORMAT_VERSION: u32 = 1;
pub const MAGIC: &[u8; 4] = b"SMPK";
pub const HEADER_LEN: usize = 24;
/// Stretches decoded in parallel when the length is known.
pub const SEGMENTS: u64 = 8;

/// `~/.cache/hindsight/peaks` (or under `$XDG_CACHE_HOME`).
pub fn default_dir() -> PathBuf {
    cache_dir().join("peaks")
}

/// Reduces frames to (min, max) bins of 1/50s of the frames' own rate.
struct Bins {
    rate: u64,
    frame: u64,
    bin: u64,
    lo: i16,
    hi: i16,
    out: Vec<u8>,
}

impl Bins {
    fn new(rate: u32, capacity: usize) -> Bins {
        Bins {
            rate: u64::from(rate),
            frame: 0,
            bin: 0,
            lo: i16::MAX,
            hi: i16::MIN,
            out: Vec::with_capacity(capacity * 2),
        }
    }

    fn push(&mut self, frame: &[i16]) {
        let bin = self.frame * u64::from(PEAK_RATE) / self.rate;
        if bin != self.bin {
            self.close();
            self.bin = bin;
        }
        let mean = frame.iter().map(|&s| i32::from(s)).sum::<i32>() / frame.len().max(1) as i32;
        let v = mean as i16;
        self.lo = self.lo.min(v);
        self.hi = self.hi.max(v);
        self.frame += 1;
    }

    fn close(&mut self) {
        if self.lo <= self.hi {
            self.out.push((self.lo >> 8) as u8);
            self.out.push((self.hi >> 8) as u8);
        }
        (self.lo, self.hi) = (i16::MAX, i16::MIN);
    }

    /// The pairs: exactly `count` (past the decoded audio is silence), or only the bins the
    /// frames filled completely.
    fn finish(mut self, count: Option<usize>) -> Vec<u8> {
        let whole = (self.frame * u64::from(PEAK_RATE) / self.rate) as usize;
        self.close();
        let n = count.unwrap_or(whole);
        self.out.resize(n * 2, 0);
        self.out
    }
}

fn header(count: usize, duration: f64) -> Vec<u8> {
    let mut h = Vec::with_capacity(HEADER_LEN);
    h.extend_from_slice(MAGIC);
    h.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    h.extend_from_slice(&PEAK_RATE.to_le_bytes());
    h.extend_from_slice(&(count as u32).to_le_bytes());
    h.extend_from_slice(&duration.to_le_bytes());
    h
}

/// `count` bins from second `start`, or to the end.
fn stretch(path: &Path, start: u64, count: Option<usize>) -> Result<Vec<u8>, AudioError> {
    let mut dec = Decoder::open(path)?;
    let rate = dec.rate();
    let mut bins = Bins::new(rate, count.unwrap_or(0));
    let frames = count.map_or(usize::MAX, |n| n * rate as usize / PEAK_RATE as usize);
    dec.for_each_frame(start * u64::from(rate), frames, start > 0, |f| bins.push(f))?;
    Ok(bins.finish(count))
}

/// The encoded peaks for `path`: with `duration`, `ceil(duration * 50)` pairs decoded in
/// [`SEGMENTS`] parallel stretches; without, one pass and the whole bins it fills.
pub fn compute_peaks(path: &Path, duration: Option<f64>) -> Result<Vec<u8>, AudioError> {
    let Some(duration) = duration.filter(|d| *d > 0.0) else {
        let pairs = stretch(path, 0, None)?;
        let n = pairs.len() / 2;
        let mut out = header(n, n as f64 / f64::from(PEAK_RATE));
        out.extend(pairs);
        return Ok(out);
    };
    let total = (duration * f64::from(PEAK_RATE)).ceil() as u64;
    let per = (duration / SEGMENTS as f64).ceil() as u64;
    let plan: Vec<(u64, usize)> = (0..SEGMENTS)
        .map(|i| (i * per, i * per * u64::from(PEAK_RATE)))
        .take_while(|&(_, first)| first < total)
        .map(|(start, first)| {
            (
                start,
                (per * u64::from(PEAK_RATE)).min(total - first) as usize,
            )
        })
        .collect();
    let parts: Vec<Result<Vec<u8>, AudioError>> = std::thread::scope(|s| {
        let handles: Vec<_> = plan
            .iter()
            .map(|&(start, n)| s.spawn(move || stretch(path, start, Some(n))))
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().expect("a peaks thread panicked"))
            .collect()
    });
    let mut out = header(total as usize, duration);
    for part in parts {
        out.extend(part?);
    }
    Ok(out)
}

/// Python's key: the resolved path, mtime in ns, size, rate and version, joined by `\x1f`.
pub fn cache_key(path: &Path) -> std::io::Result<String> {
    let st = fs::metadata(path)?;
    let mtime_ns = i128::from(st.mtime()) * 1_000_000_000 + i128::from(st.mtime_nsec());
    let raw = [
        fs::canonicalize(path)?.to_string_lossy().into_owned(),
        mtime_ns.to_string(),
        st.len().to_string(),
        PEAK_RATE.to_string(),
        FORMAT_VERSION.to_string(),
    ]
    .join("\x1f");
    Ok(crate::sha256_hex(raw.as_bytes()))
}

static LOCKS: LazyLock<Mutex<HashMap<String, Arc<Mutex<()>>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// Cached [`compute_peaks`], one computation per file at a time: a reload during the first
/// waits for it rather than starting a second. An unwritable cache still answers.
pub fn peaks_for(path: &Path, duration: Option<f64>, dir: &Path) -> Result<Vec<u8>, AudioError> {
    let key = cache_key(path)?;
    let target = dir.join(format!("{key}.peaks"));
    let lock = LOCKS
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .entry(key)
        .or_default()
        .clone();
    let _held = lock.lock().unwrap_or_else(|e| e.into_inner());
    if let Ok(data) = fs::read(&target)
        && data.len() >= HEADER_LEN
        && data.starts_with(MAGIC)
    {
        return Ok(data);
    }
    let data = compute_peaks(path, duration)?;
    if let Err(e) = atomic_write(&target, &data) {
        log::debug!("peaks cache write skipped for {}: {e}", target.display());
    }
    Ok(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("src/audio/fixtures")
            .join(name)
    }

    fn parse(data: &[u8]) -> (u32, u32, u32, f64, &[u8]) {
        let u = |i: usize| u32::from_le_bytes(data[i..i + 4].try_into().unwrap());
        assert_eq!(&data[..4], MAGIC);
        let d = f64::from_le_bytes(data[16..24].try_into().unwrap());
        (u(4), u(8), u(12), d, &data[HEADER_LEN..])
    }

    #[test]
    fn peaks_cover_the_clip_at_the_peak_rate() {
        let data = compute_peaks(&fixture("stereo48_cbr.mp3"), Some(12.024)).unwrap();
        let (version, rate, count, duration, pairs) = parse(&data);
        assert_eq!((version, rate, duration), (1, 50, 12.024));
        assert_eq!(count, 602, "ceil(12.024 * 50)");
        assert_eq!(pairs.len(), 602 * 2);
        // A loud signal: every bin of the audio has some spread, min below max.
        let full = &pairs[..600 * 2];
        assert!(full.chunks(2).all(|p| (p[0] as i8) < (p[1] as i8)));
        // The last bin is the 0.2 of one that the audio fills; past the audio is silence.
        let long = compute_peaks(&fixture("stereo48_cbr.mp3"), Some(13.0)).unwrap();
        let (_, _, count, _, pairs) = parse(&long);
        assert_eq!(count, 650);
        assert_ne!(&pairs[601 * 2..602 * 2], [0, 0]);
        assert!(pairs[602 * 2..].iter().all(|&b| b == 0));
    }

    #[test]
    fn parallel_stretches_line_up_with_a_single_pass() {
        for name in ["stereo48_cbr.mp3", "mono44_info.mp3", "tone8k.flac"] {
            let single = compute_peaks(&fixture(name), None).unwrap();
            let (_, _, n, _, one) = parse(&single);
            let split = compute_peaks(&fixture(name), Some(f64::from(n) / 50.0)).unwrap();
            let (_, _, m, _, many) = parse(&split);
            assert_eq!(n, m, "{name}");
            // Seeking with the preroll is sample-exact (spike 2), so the seams don't show.
            assert_eq!(one, many, "{name}");
        }
    }

    #[test]
    fn the_high_byte_is_kept_as_python_keeps_it() {
        let mut b = Bins::new(50, 2);
        b.push(&[-32768, -32768]);
        b.push(&[300, 300]);
        b.push(&[-1, 1]);
        let out = b.finish(Some(3));
        assert_eq!(out, [0x80, 0x80, 0x01, 0x01, 0x00, 0x00]);
        // Python: (-32768 >> 8) & 0xFF == 0x80, (300 >> 8) & 0xFF == 1, mean(-1, 1) == 0.
    }

    #[test]
    fn peaks_are_cached_until_the_file_changes() {
        let dir = tempfile::tempdir().unwrap();
        let clip = dir.path().join("clip.mp3");
        fs::copy(fixture("stereo48_cbr.mp3"), &clip).unwrap();
        let cache = dir.path().join("peaks");
        let first = peaks_for(&clip, Some(12.024), &cache).unwrap();
        let key = cache_key(&clip).unwrap();
        assert_eq!(fs::read(cache.join(format!("{key}.peaks"))).unwrap(), first);

        // A doctored cache entry is served as is: the key, not the contents, is the check.
        let mut doctored = first.clone();
        *doctored.last_mut().unwrap() ^= 0xff;
        fs::write(cache.join(format!("{key}.peaks")), &doctored).unwrap();
        assert_eq!(peaks_for(&clip, Some(12.024), &cache).unwrap(), doctored);

        // Touching the file is a new key.
        let later = std::time::SystemTime::now() + std::time::Duration::from_secs(5);
        fs::File::options()
            .write(true)
            .open(&clip)
            .unwrap()
            .set_modified(later)
            .unwrap();
        assert_ne!(cache_key(&clip).unwrap(), key);
        assert_eq!(peaks_for(&clip, Some(12.024), &cache).unwrap(), first);
    }
}
