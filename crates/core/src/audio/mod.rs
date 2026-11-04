//! A recording, decoded with symphonia: its length, the decode-completeness guard, and the
//! windows a run sends to Shazam.
//!
//! A window is prepared the way shazamio-core prepares audio (spike 1): decoded to i16 with
//! gapless trimming, the **first channel only** (rodio's mono conversion drops the rest), then
//! resampled to 16 kHz by rodio's integer linear interpolation. Seeking starts one MP3 frame
//! early and throws it away: straight to the frame, the decoder's warm-up changes up to the
//! first 22ms, and with the preroll a seeked window is sample-identical to a continuous decode
//! (spike 2).

mod decoder;
mod mp3;
pub mod peaks;

use std::fmt;
use std::path::{Path, PathBuf};

pub use decoder::Decoder;

/// One MP3 frame: how far before a window the decoder starts.
pub const PREROLL: u64 = 1152;

/// The guard trips only when less than this share of the stated length decoded...
pub const DECODE_SHORTFALL_REL_TOLERANCE: f64 = 0.02;
/// ...and the gap is more than this many seconds, so frame padding never trips it.
pub const DECODE_SHORTFALL_MIN_GAP_SECONDS: f64 = 2.0;

#[derive(Debug)]
pub enum AudioError {
    /// The file can't be opened or decoded.
    Unreadable(String),
    /// Far less decoded than the file says it holds: the signature of a file still being
    /// written or synced when it was read.
    Truncated { decoded_s: f64, stated_s: f64 },
}

impl fmt::Display for AudioError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AudioError::Unreadable(why) => write!(f, "can't read the audio: {why}"),
            AudioError::Truncated {
                decoded_s,
                stated_s,
            } => write!(
                f,
                "decoded only {} of audio, but the file says it holds {}. It was likely still \
                 being written or downloading (e.g. iCloud sync) when it was read. Wait for it \
                 to finish, then try again.",
                format_timestamp(*decoded_s as i64),
                format_timestamp(*stated_s as i64)
            ),
        }
    }
}

impl std::error::Error for AudioError {}

impl From<symphonia::core::errors::Error> for AudioError {
    fn from(e: symphonia::core::errors::Error) -> Self {
        AudioError::Unreadable(e.to_string())
    }
}

impl From<std::io::Error> for AudioError {
    fn from(e: std::io::Error) -> Self {
        AudioError::Unreadable(e.to_string())
    }
}

/// `M:SS`, or `H:MM:SS` from an hour on.
pub fn format_timestamp(seconds: i64) -> String {
    let (h, m, s) = (seconds / 3600, seconds % 3600 / 60, seconds % 60);
    if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}

/// A recording whose length has been measured by decoding all of it.
#[derive(Clone, Debug)]
pub struct Recording {
    path: PathBuf,
    rate: u32,
    frames: u64,
    /// The length the container states, when it states one (see [`Decoder::stated_frames`]).
    stated_frames: Option<u64>,
}

impl Recording {
    /// Decodes the whole file to measure it, which is the length the engine plans against
    /// (Python's was the decoded length too): ~10s for a 4-hour MP3. Unless `allow_partial`,
    /// refuses a file that decodes well short of the length it states.
    pub fn open(path: &Path, allow_partial: bool) -> Result<Recording, AudioError> {
        let mut dec = Decoder::open(path)?;
        let (rate, stated_frames) = (dec.rate(), dec.stated_frames());
        let frames = dec.count_remaining_frames()?;
        let rec = Recording {
            path: path.to_path_buf(),
            rate,
            frames,
            stated_frames,
        };
        if !allow_partial {
            verify_decode_complete(rec.decoded_seconds(), rec.stated_seconds())?;
        }
        Ok(rec)
    }

    pub fn rate(&self) -> u32 {
        self.rate
    }

    pub fn frames(&self) -> u64 {
        self.frames
    }

    pub fn decoded_seconds(&self) -> f64 {
        self.frames as f64 / self.rate as f64
    }

    pub fn stated_seconds(&self) -> Option<f64> {
        self.stated_frames.map(|n| n as f64 / self.rate as f64)
    }

    /// The length a run records and plans against, in seconds, rounded to the millisecond as
    /// pydub's `len()` was, so a resumed Python run folds against the same duration.
    pub fn duration(&self) -> f64 {
        (1000.0 * self.decoded_seconds()).round_ties_even() / 1000.0
    }

    /// `[start, start + window)` of the first channel at the file's own rate, clamped to the
    /// recording, so a window near the end comes back short.
    pub fn window_first_channel(&self, start: f64, window: f64) -> Result<Vec<i16>, AudioError> {
        let first = ((start.max(0.0) * self.rate as f64).round() as u64).min(self.frames);
        let count = ((window.max(0.0) * self.rate as f64).round() as u64).min(self.frames - first);
        let mut dec = Decoder::open(&self.path)?;
        dec.first_channel(first, count as usize, true)
    }

    /// The window as Shazam's signature wants it: first channel, 16 kHz.
    pub fn window_16k(&self, start: f64, window: f64) -> Result<Vec<i16>, AudioError> {
        let pcm = self.window_first_channel(start, window)?;
        Ok(resample_linear(&pcm, self.rate, crate::shazam::SAMPLE_RATE))
    }
}

/// A recording's length in seconds from its packets, without decoding it ([`Decoder::
/// count_remaining_frames_undecoded`]): what the chapters embed ends the last chapter at.
pub fn undecoded_seconds(path: &Path) -> Result<f64, AudioError> {
    let mut dec = Decoder::open(path)?;
    let frames = dec.count_remaining_frames_undecoded()?;
    Ok(frames as f64 / dec.rate() as f64)
}

/// Fails when the decode fell well short of the length the container states. Nothing stated,
/// nothing to compare: the guard is skipped, as Python's was when ffprobe had no answer.
pub fn verify_decode_complete(decoded_s: f64, stated_s: Option<f64>) -> Result<(), AudioError> {
    let Some(stated_s) = stated_s.filter(|s| *s > 0.0) else {
        return Ok(());
    };
    if stated_s - decoded_s <= DECODE_SHORTFALL_MIN_GAP_SECONDS
        || decoded_s >= stated_s * (1.0 - DECODE_SHORTFALL_REL_TOLERANCE)
    {
        return Ok(());
    }
    Err(AudioError::Truncated {
        decoded_s,
        stated_s,
    })
}

/// rodio 0.20's `SampleRateConverter` arithmetic, run continuously: output sample `k` sits at
/// input position `k * from / to` (reduced by their gcd) and is the integer lerp of its two
/// neighbours.
pub fn resample_linear(input: &[i16], from: u32, to: u32) -> Vec<i16> {
    if from == to {
        return input.to_vec();
    }
    let g = gcd(from, to);
    let (from, to) = ((from / g) as u64, (to / g) as u64);
    let mut out = Vec::with_capacity((input.len() as u64 * to / from) as usize + 1);
    for k in 0.. {
        let left = (k * from / to) as usize;
        if left + 1 >= input.len() {
            break;
        }
        let num = (k * from % to) as i32;
        let (a, b) = (input[left] as i32, input[left + 1] as i32);
        out.push((a + (b - a) * num / to as i32) as i16);
    }
    out
}

fn gcd(a: u32, b: u32) -> u32 {
    if b == 0 { a } else { gcd(b, a % b) }
}

#[cfg(test)]
mod tests;
