//! symphonia 0.5 behind `Source` (the version rodio 0.20.1, and so shazamio-core, decodes with).

use crate::source::{Error, Info, Source};
use std::fs::File;
use std::path::Path;
use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::{Decoder, DecoderOptions, CODEC_TYPE_NULL};
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::{FormatOptions, FormatReader, SeekMode, SeekTo};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;

pub struct Dec {
    format: Box<dyn FormatReader>,
    decoder: Box<dyn Decoder>,
    track: u32,
    info: Info,
    buf: Option<SampleBuffer<i16>>,
    errors: u64,
}

// The boxed reader and decoder are only ever used from the thread that owns them.
unsafe impl Send for Dec {}

impl Dec {
    pub fn open(path: &Path) -> Result<Self, Error> {
        let mss = MediaSourceStream::new(Box::new(File::open(path)?), Default::default());
        let mut hint = Hint::new();
        if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
            hint.with_extension(ext);
        }
        let fmt_opts = FormatOptions { enable_gapless: true, ..Default::default() };
        let probed =
            symphonia::default::get_probe().format(&hint, mss, &fmt_opts, &MetadataOptions::default())?;
        let format = probed.format;
        // Not `default_track()`: pick the first track that is actually audio.
        let track = format
            .tracks()
            .iter()
            .find(|t| t.codec_params.codec != CODEC_TYPE_NULL && t.codec_params.sample_rate.is_some())
            .ok_or("no audio track")?
            .clone();
        let p = &track.codec_params;
        let rate = p.sample_rate.ok_or("unknown sample rate")?;
        let reported_s = p.n_frames.map(|n| match p.time_base {
            Some(tb) => {
                let t = tb.calc_time(n);
                t.seconds as f64 + t.frac
            }
            None => n as f64 / rate as f64,
        });
        let codec = symphonia::default::get_codecs()
            .get_codec(p.codec)
            .map(|d| d.short_name.to_string())
            .unwrap_or_else(|| format!("{:?}", p.codec));
        let info = Info {
            codec,
            rate,
            channels: p.channels.map(|c| c.count()).unwrap_or(0),
            bits: p.bits_per_sample,
            n_frames: p.n_frames,
            delay: p.delay,
            padding: p.padding,
            reported_s,
        };
        let decoder = symphonia::default::get_codecs().make(p, &DecoderOptions::default())?;
        let mut dec = Dec { format, decoder, track: track.id, info, buf: None, errors: 0 };
        // 0.5's MP4 reader leaves the channel count unset (0.6 fills it in): learn it from
        // the first decoded packet, then rewind so callers start at the top.
        if dec.info.channels == 0 {
            dec.next_chunk(&mut Vec::new())?;
            dec.seek(0)?;
        }
        Ok(dec)
    }
}

impl Source for Dec {
    fn info(&self) -> &Info {
        &self.info
    }

    fn seek(&mut self, frame: u64) -> Result<i64, Error> {
        let to = SeekTo::TimeStamp { ts: frame, track_id: self.track };
        let seeked = self.format.seek(SeekMode::Accurate, to)?;
        self.decoder.reset();
        Ok(seeked.actual_ts as i64)
    }

    fn next_chunk(&mut self, out: &mut Vec<i16>) -> Result<Option<i64>, Error> {
        loop {
            let packet = match self.format.next_packet() {
                Ok(p) => p,
                Err(SymError::IoError(e)) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                    return Ok(None)
                }
                Err(e) => return Err(e.into()),
            };
            if packet.track_id() != self.track {
                continue;
            }
            let decoded = match self.decoder.decode(&packet) {
                Ok(d) => d,
                Err(SymError::DecodeError(_)) => {
                    self.errors += 1;
                    continue;
                }
                Err(e) => return Err(e.into()),
            };
            let spec = *decoded.spec();
            if self.info.channels == 0 {
                self.info.channels = spec.channels.count();
            }
            let cap = decoded.capacity();
            let b = self.buf.get_or_insert_with(|| SampleBuffer::new(cap as u64, spec));
            if b.capacity() < cap * spec.channels.count() {
                *b = SampleBuffer::new(cap as u64, spec);
            }
            b.copy_interleaved_ref(decoded);
            out.clear();
            out.extend_from_slice(b.samples());
            return Ok(Some(packet.ts() as i64));
        }
    }

    fn decode_errors(&self) -> u64 {
        self.errors
    }
}
