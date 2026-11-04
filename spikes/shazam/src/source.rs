//! M0 spike 2: one interface over symphonia 0.5 and 0.6, so every measurement runs against
//! both and their PCM can be diffed.

pub type Error = Box<dyn std::error::Error + Send + Sync>;

/// What the container claims before anything is decoded: the decode guard's "reported"
/// side, which Python got from ffprobe.
#[derive(Debug, Clone, serde::Serialize)]
pub struct Info {
    pub codec: String,
    pub rate: u32,
    pub channels: usize,
    pub bits: Option<u32>,
    /// Frames the container says the track has (after gapless trimming, where known).
    pub n_frames: Option<u64>,
    pub delay: Option<u32>,
    pub padding: Option<u32>,
    pub reported_s: Option<f64>,
}

/// A decoder positioned somewhere in a file, handing out interleaved i16 chunks.
pub trait Source: Send {
    fn info(&self) -> &Info;
    /// Accurate seek to `frame`. Returns the frame the demuxer says it landed before.
    fn seek(&mut self, frame: u64) -> Result<i64, Error>;
    /// The next decoded chunk into `out` (interleaved), with the absolute frame index of
    /// its first frame. `None` at end of stream. Decode errors are counted and skipped.
    fn next_chunk(&mut self, out: &mut Vec<i16>) -> Result<Option<i64>, Error>;
    fn decode_errors(&self) -> u64;
}

/// One MP3 frame. Seeking this far early and discarding it makes a seeked MP3 window
/// sample-identical to a continuous decode (spike 2).
pub const PREROLL: u64 = 1152;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Version {
    V05,
    V06,
}

pub fn open(v: Version, path: &std::path::Path) -> Result<Box<dyn Source>, Error> {
    match v {
        Version::V05 => Ok(Box::new(crate::dec05::Dec::open(path)?)),
        Version::V06 => Ok(Box::new(crate::dec06::Dec::open(path)?)),
    }
}

/// `count` frames of every channel starting at `first`, by seeking to `first - preroll`
/// (when `seek`) and discarding everything before `first`. `seek` false decodes from the top.
pub fn window(
    src: &mut dyn Source,
    first: u64,
    count: usize,
    seek: bool,
    preroll: u64,
) -> Result<Vec<i16>, Error> {
    let ch = src.info().channels;
    if seek && first > 0 {
        src.seek(first.saturating_sub(preroll))?;
    }
    let mut out = Vec::with_capacity(count * ch);
    let mut buf = Vec::new();
    while out.len() < count * ch {
        let Some(ts) = src.next_chunk(&mut buf)? else { break };
        for (i, frame) in buf.chunks(ch).enumerate() {
            let at = ts + i as i64;
            if at < first as i64 {
                continue;
            }
            if out.len() == count * ch {
                break;
            }
            out.extend_from_slice(frame);
        }
    }
    Ok(out)
}

/// FNV-1a over the samples' little-endian bytes: cheap, and enough to say "identical".
#[derive(Clone, Copy)]
pub struct Fnv(pub u64);

impl Default for Fnv {
    fn default() -> Self {
        Fnv(0xcbf29ce484222325)
    }
}

impl Fnv {
    pub fn add(&mut self, samples: &[i16]) {
        for s in samples {
            for b in s.to_le_bytes() {
                self.0 ^= b as u64;
                self.0 = self.0.wrapping_mul(0x100000001b3);
            }
        }
    }
}

/// How two equal-length runs of samples differ.
#[derive(Debug, Default, serde::Serialize)]
pub struct Diff {
    pub len: [usize; 2],
    pub differing: usize,
    pub first: Option<usize>,
    pub last: Option<usize>,
    pub max_abs: i32,
}

pub fn diff(a: &[i16], b: &[i16]) -> Diff {
    let mut d = Diff { len: [a.len(), b.len()], ..Default::default() };
    for (i, (x, y)) in a.iter().zip(b).enumerate() {
        if x != y {
            d.differing += 1;
            d.first.get_or_insert(i);
            d.last = Some(i);
            d.max_abs = d.max_abs.max((*x as i32 - *y as i32).abs());
        }
    }
    d
}
