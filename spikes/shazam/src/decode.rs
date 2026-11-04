//! Decode one window of a long recording to 16 kHz mono i16, the way shazamio-core
//! (via rodio 0.20.1) prepares audio: symphonia with gapless trimming, samples converted
//! to i16, the **first channel only** (rodio's `ChannelCountConverter` discards the rest
//! when going to mono), then integer linear interpolation to 16 kHz.
//!
//! Rodio restarts its resampler on every decoded packet; that is an artifact and is not
//! reproduced here (it shifts timing by well under the spike's 0.1s budget).

use std::fs::File;
use std::path::Path;
use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::DecoderOptions;
use symphonia::core::formats::{FormatOptions, SeekMode, SeekTo};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;
use symphonia::core::units::Time;

pub type Error = Box<dyn std::error::Error>;

/// `[start_s, start_s + window_s)` of `path`, as 16 kHz mono. `seek` false decodes from the
/// top of the file instead, to check that seeking lands on the same samples.
pub fn window_16k_mono(
    path: &Path,
    start_s: f64,
    window_s: f64,
    seek: bool,
) -> Result<Vec<i16>, Error> {
    let (rate, left) = window_first_channel(path, start_s, window_s, seek)?;
    Ok(resample_linear(&left, rate, crate::signature::SAMPLE_RATE))
}

/// The window's first channel at the file's own rate.
pub fn window_first_channel(
    path: &Path,
    start_s: f64,
    window_s: f64,
    seek: bool,
) -> Result<(u32, Vec<i16>), Error> {
    let mss = MediaSourceStream::new(Box::new(File::open(path)?), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let fmt_opts = FormatOptions {
        enable_gapless: true,
        ..Default::default()
    };
    let probed = symphonia::default::get_probe().format(
        &hint,
        mss,
        &fmt_opts,
        &MetadataOptions::default(),
    )?;
    let mut format = probed.format;
    let track = format.default_track().ok_or("no audio track")?.clone();
    let rate = track
        .codec_params
        .sample_rate
        .ok_or("unknown sample rate")?;
    let mut decoder =
        symphonia::default::get_codecs().make(&track.codec_params, &DecoderOptions::default())?;

    let first = (start_s * rate as f64).round() as u64;
    let wanted = (window_s * rate as f64).round() as usize;
    if seek && first > 0 {
        let secs = first / rate as u64;
        let frac = (first % rate as u64) as f64 / rate as f64;
        format.seek(
            SeekMode::Accurate,
            SeekTo::Time {
                time: Time::new(secs, frac),
                track_id: Some(track.id),
            },
        )?;
        decoder.reset();
    }

    let mut out: Vec<i16> = Vec::with_capacity(wanted);
    let mut buf: Option<SampleBuffer<i16>> = None;
    while out.len() < wanted {
        let packet = match format.next_packet() {
            Ok(p) => p,
            Err(symphonia::core::errors::Error::IoError(e))
                if e.kind() == std::io::ErrorKind::UnexpectedEof =>
            {
                break
            }
            Err(e) => return Err(e.into()),
        };
        if packet.track_id() != track.id {
            continue;
        }
        let decoded = match decoder.decode(&packet) {
            Ok(d) => d,
            Err(symphonia::core::errors::Error::DecodeError(_)) => continue,
            Err(e) => return Err(e.into()),
        };
        let spec = *decoded.spec();
        let channels = spec.channels.count();
        let b = buf.get_or_insert_with(|| SampleBuffer::new(decoded.capacity() as u64, spec));
        if b.capacity() < decoded.capacity() * channels {
            *b = SampleBuffer::new(decoded.capacity() as u64, spec);
        }
        b.copy_interleaved_ref(decoded);
        let ts = packet.ts();
        for (i, frame) in b.samples().chunks(channels).enumerate() {
            let at = ts + i as u64;
            if at < first {
                continue;
            }
            if out.len() == wanted {
                break;
            }
            out.push(frame[0]);
        }
    }
    Ok((rate, out))
}

/// rodio 0.20's `SampleRateConverter` arithmetic, run continuously: output sample `k`
/// sits at input position `k * from / to` (reduced by their gcd) and is the integer lerp
/// of its two neighbours.
pub fn resample_linear(input: &[i16], from: u32, to: u32) -> Vec<i16> {
    if from == to {
        return input.to_vec();
    }
    let g = gcd(from, to);
    let (from, to) = ((from / g) as u64, (to / g) as u64);
    let mut out = Vec::with_capacity((input.len() as u64 * to / from) as usize + 1);
    let mut k: u64 = 0;
    loop {
        let left = (k * from / to) as usize;
        if left + 1 >= input.len() {
            break;
        }
        let num = (k * from % to) as i32;
        let (a, b) = (input[left] as i32, input[left + 1] as i32);
        out.push((a + (b - a) * num / to as i32) as i16);
        k += 1;
    }
    out
}

fn gcd(a: u32, b: u32) -> u32 {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}
