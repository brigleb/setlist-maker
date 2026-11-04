//! Shazam audio signature generation.
//!
//! The algorithm and the wire format are SongRec's `SignatureGenerator` and
//! `DecodedSignature` by Marin M. (GPL-3.0-or-later, https://github.com/marin-m/SongRec,
//! `src/core/fingerprinting/`), which is why Hindsight is GPL-3
//! (docs/adr/0001-gpl-3-via-songrec.md). shazamio-core 1.1.2 ships the same code; checked
//! against SongRec c6cd35b, the peak rules, constants and Hann table are identical.
//!
//! One deliberate difference from current SongRec: the FFT is `chfft` (as in shazamio-core),
//! not `realfft`. That is what makes the output byte-identical to shazamio-core's, which
//! is what the Python version sends (`tests/fingerprint.rs`). Whether `realfft` gives the
//! same bits is unverified. Only the centred excerpt below is shazamio-core's own.

use crate::hanning::HANNING_WINDOW_2048_MULTIPLIERS;
use base64::{engine::general_purpose::STANDARD, Engine};
use chfft::RFft1D;
use std::collections::BTreeMap;

pub const SAMPLE_RATE: u32 = 16000;
/// shazamio-core's `SearchParams.segment_duration_seconds` default: Shazam hears this many
/// seconds from the middle of whatever window it is handed (the excerpt).
pub const EXCERPT_SECONDS: u32 = 10;

const DATA_URI_PREFIX: &str = "data:audio/vnd.shazam.sig;base64,";

/// What Shazam is sent for one window.
pub struct Fingerprint {
    /// `data:audio/vnd.shazam.sig;base64,...`, the request's `signature.uri`.
    pub uri: String,
    /// The excerpt's length in ms, the request's `signature.samplems`.
    pub excerpt_ms: u32,
    /// Seconds from the window's start to the excerpt's: `(window - 10) / 2`, or 0.
    pub lead_s: f64,
}

/// Centre a 16 kHz mono window on its middle 10 seconds and fingerprint that excerpt.
pub fn fingerprint(window_16k_mono: &[i16]) -> Fingerprint {
    let ex = excerpt(window_16k_mono);
    let sig = Signature::generate(ex);
    Fingerprint {
        uri: format!("{DATA_URI_PREFIX}{}", STANDARD.encode(sig.encode())),
        excerpt_ms: (ex.len() as f32 / SAMPLE_RATE as f32 * 1000.) as u32,
        lead_s: (window_16k_mono.len() - ex.len()) as f64 / 2.0 / SAMPLE_RATE as f64,
    }
}

/// The excerpt shazamio-core fingerprints: the middle 10s, or everything when shorter.
/// Same arithmetic as `make_signature_from_file` (`middle = len / 2`).
fn excerpt(pcm: &[i16]) -> &[i16] {
    let segment = (EXCERPT_SECONDS * SAMPLE_RATE) as usize;
    if pcm.len() <= segment {
        return pcm;
    }
    let middle = pcm.len() / 2;
    &pcm[middle - segment / 2..middle + segment / 2]
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Band {
    B250_520 = 0,
    B520_1450 = 1,
    B1450_3500 = 2,
    B3500_5500 = 3,
}

struct Peak {
    fft_pass: u32,
    magnitude: u16,
    corrected_bin: u16,
}

struct Signature {
    number_samples: u32,
    peaks: BTreeMap<Band, Vec<Peak>>,
}

impl Signature {
    fn generate(samples: &[i16]) -> Signature {
        let mut g = Generator {
            ring: vec![0; 2048],
            ring_index: 0,
            reordered: vec![0.0; 2048],
            fft_outputs: vec![vec![0.0; 1025]; 256],
            fft_index: 0,
            fft: RFft1D::new(2048),
            spread: vec![vec![0.0; 1025]; 256],
            spread_index: 0,
            spread_done: 0,
            sig: Signature {
                number_samples: samples.len() as u32,
                peaks: BTreeMap::new(),
            },
        };
        for chunk in samples.as_chunks::<128>().0 {
            g.do_fft(chunk);
            g.do_peak_spreading();
            g.spread_done += 1;
            if g.spread_done >= 46 {
                g.do_peak_recognition();
            }
        }
        g.sig
    }

    fn encode(&self) -> Vec<u8> {
        let mut b: Vec<u8> = Vec::new();
        let put = |b: &mut Vec<u8>, v: u32| b.extend_from_slice(&v.to_le_bytes());
        put(&mut b, 0xcafe2580); // magic1
        put(&mut b, 0); // crc32, filled in below
        put(&mut b, 0); // size minus header, filled in below
        put(&mut b, 0x94119c00); // magic2
        for _ in 0..3 {
            put(&mut b, 0);
        }
        put(&mut b, 3 << 27); // sample rate id: 16000 Hz
        for _ in 0..2 {
            put(&mut b, 0);
        }
        put(&mut b, self.number_samples + (SAMPLE_RATE as f32 * 0.24) as u32);
        put(&mut b, (15 << 19) + 0x40000);
        put(&mut b, 0x40000000);
        put(&mut b, 0); // size minus header again, filled in below

        for (band, peaks) in &self.peaks {
            let mut p: Vec<u8> = Vec::new();
            let mut pass = 0u32;
            for peak in peaks {
                if peak.fft_pass - pass >= 255 {
                    p.push(0xff);
                    p.extend_from_slice(&peak.fft_pass.to_le_bytes());
                    pass = peak.fft_pass;
                }
                p.push((peak.fft_pass - pass) as u8);
                p.extend_from_slice(&peak.magnitude.to_le_bytes());
                p.extend_from_slice(&peak.corrected_bin.to_le_bytes());
                pass = peak.fft_pass;
            }
            put(&mut b, 0x60030040 + *band as u32);
            put(&mut b, p.len() as u32);
            let padding = (4 - p.len() % 4) % 4;
            b.extend_from_slice(&p);
            b.extend(std::iter::repeat_n(0, padding));
        }

        let size = (b.len() - 48) as u32;
        b[8..12].copy_from_slice(&size.to_le_bytes());
        b[52..56].copy_from_slice(&size.to_le_bytes());
        let crc = crc32fast::hash(&b[8..]);
        b[4..8].copy_from_slice(&crc.to_le_bytes());
        b
    }
}

struct Generator {
    ring: Vec<i16>,
    ring_index: usize,
    reordered: Vec<f32>,
    fft_outputs: Vec<Vec<f32>>,
    fft_index: usize,
    fft: RFft1D<f32>,
    spread: Vec<Vec<f32>>,
    spread_index: usize,
    spread_done: u32,
    sig: Signature,
}

/// The entry `offset` places behind (negative) or ahead of `i` in a 256-entry ring.
fn ring256(i: usize, offset: i32) -> usize {
    ((i as i32 + offset) & 255) as usize
}

impl Generator {
    fn do_fft(&mut self, chunk: &[i16]) {
        self.ring[self.ring_index..self.ring_index + 128].copy_from_slice(chunk);
        self.ring_index = (self.ring_index + 128) & 2047;

        for (i, m) in HANNING_WINDOW_2048_MULTIPLIERS.iter().enumerate() {
            self.reordered[i] = self.ring[(i + self.ring_index) & 2047] as f32 * m;
        }
        let complex = self.fft.forward(&self.reordered);
        let out = &mut self.fft_outputs[self.fft_index];
        for i in 0..=1024 {
            out[i] = ((complex[i].re.powi(2) + complex[i].im.powi(2)) / ((1 << 17) as f32))
                .max(0.0000000001);
        }
        self.fft_index = (self.fft_index + 1) & 255;
    }

    fn do_peak_spreading(&mut self) {
        let latest = self.fft_outputs[ring256(self.fft_index, -1)].clone();
        let spread = &mut self.spread[self.spread_index];
        spread.copy_from_slice(&latest);
        for p in 0..=1022 {
            spread[p] = spread[p].max(spread[p + 1]).max(spread[p + 2]);
        }
        let spread_copy = spread.clone();
        for p in 0..=1024 {
            for former in [1, 3, 6] {
                let f = &mut self.spread[ring256(self.spread_index, -former)];
                f[p] = f[p].max(spread_copy[p]);
            }
        }
        self.spread_index = (self.spread_index + 1) & 255;
    }

    fn do_peak_recognition(&mut self) {
        let fft46 = &self.fft_outputs[ring256(self.fft_index, -46)];
        let spread49 = &self.spread[ring256(self.spread_index, -49)];

        for bin in 10..=1014 {
            if !(fft46[bin] >= 1.0 / 64.0 && fft46[bin] >= spread49[bin - 1]) {
                continue;
            }
            // Frequency-domain local maximum.
            let mut max_neighbor: f32 = 0.0;
            for off in [-10, -7, -4, -3, 1, 2, 5, 8] {
                max_neighbor = max_neighbor.max(spread49[(bin as i32 + off) as usize]);
            }
            if fft46[bin] <= max_neighbor {
                continue;
            }
            // Time-domain local maximum.
            let mut max_other = max_neighbor;
            for off in [
                -53, -45, 165, 172, 179, 186, 193, 200, 214, 221, 228, 235, 242, 249,
            ] {
                max_other = max_other.max(self.spread[ring256(self.spread_index, off)][bin - 1]);
            }
            if fft46[bin] <= max_other {
                continue;
            }

            let mag = |v: f32| v.ln().max(1.0 / 64.0) * 1477.3 + 6144.0;
            let magnitude = mag(fft46[bin]);
            let before = mag(fft46[bin - 1]);
            let after = mag(fft46[bin + 1]);
            let variation1 = magnitude * 2.0 - before - after;
            let variation2 = (after - before) * 32.0 / variation1;
            let corrected_bin = ((bin as i32 * 64) + (variation2 as i32)) as u16;
            assert!(variation1 >= 0.0);

            let hz = corrected_bin as f32 * (16000.0 / 2.0 / 1024.0 / 64.0);
            let band = match hz as i32 {
                250..=519 => Band::B250_520,
                520..=1449 => Band::B520_1450,
                1450..=3499 => Band::B1450_3500,
                3500..=5500 => Band::B3500_5500,
                _ => continue,
            };
            self.sig.peaks.entry(band).or_default().push(Peak {
                fft_pass: self.spread_done - 46,
                magnitude: magnitude as u16,
                corrected_bin,
            });
        }
    }
}
