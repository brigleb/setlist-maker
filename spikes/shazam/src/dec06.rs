//! symphonia 0.6 behind `Source`. Gapless trimming moved from the demuxer to the decoder
//! here: packets carry `pts` (negative for the encoder delay) plus `trim_start`, so the first
//! frame a decoded buffer holds is at `pts + trim_start`.

use crate::source::{Error, Info, Source};
use std::fs::File;
use std::path::Path;
use symphonia06::core::codecs::audio::{AudioDecoder, AudioDecoderOptions};
use symphonia06::core::codecs::CodecParameters;
use symphonia06::core::errors::Error as SymError;
use symphonia06::core::formats::probe::Hint;
use symphonia06::core::formats::{FormatOptions, FormatReader, SeekMode, SeekTo};
use symphonia06::core::io::MediaSourceStream;
use symphonia06::core::meta::MetadataOptions;
use symphonia06::core::units::Timestamp;

pub struct Dec {
    format: Box<dyn FormatReader>,
    decoder: Box<dyn AudioDecoder>,
    track: u32,
    info: Info,
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
        let format = symphonia06::default::get_probe().probe(
            &hint,
            mss,
            FormatOptions::default(),
            MetadataOptions::default(),
        )?;
        let track = format
            .tracks()
            .iter()
            .find(|t| matches!(t.codec_params, Some(CodecParameters::Audio(_))))
            .ok_or("no audio track")?
            .clone();
        let Some(CodecParameters::Audio(p)) = &track.codec_params else { unreachable!() };
        let rate = p.sample_rate.ok_or("unknown sample rate")?;
        let reported_s = track.num_frames.map(|n| match track.time_base {
            Some(tb) => tb
                .calc_time(Timestamp::new(n as i64))
                .map(|t| t.as_secs_f64())
                .unwrap_or(f64::NAN),
            None => n as f64 / rate as f64,
        });
        let codec = symphonia06::default::get_codecs()
            .get_audio_decoder(p.codec)
            .map(|d| d.codec.info.short_name.to_string())
            .unwrap_or_else(|| format!("{:?}", p.codec));
        let info = Info {
            codec,
            rate,
            channels: p.channels.as_ref().map(|c| c.count()).unwrap_or(0),
            bits: p.bits_per_sample,
            n_frames: track.num_frames,
            delay: track.delay,
            padding: track.padding,
            reported_s,
        };
        let decoder = symphonia06::default::get_codecs()
            .make_audio_decoder(p, &AudioDecoderOptions::default())?;
        Ok(Dec { format, decoder, track: track.id, info, errors: 0 })
    }
}

impl Source for Dec {
    fn info(&self) -> &Info {
        &self.info
    }

    fn seek(&mut self, frame: u64) -> Result<i64, Error> {
        let to = SeekTo::Timestamp { ts: Timestamp::new(frame as i64), track_id: self.track };
        let seeked = self.format.seek(SeekMode::Accurate, to)?;
        self.decoder.reset();
        Ok(seeked.actual_ts.get())
    }

    fn next_chunk(&mut self, out: &mut Vec<i16>) -> Result<Option<i64>, Error> {
        loop {
            let packet = match self.format.next_packet() {
                Ok(Some(p)) => p,
                Ok(None) => return Ok(None),
                Err(SymError::IoError(e)) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                    return Ok(None)
                }
                Err(e) => return Err(e.into()),
            };
            if packet.track_id != self.track {
                continue;
            }
            let first = packet.pts.get() + packet.trim_start.get() as i64;
            let decoded = match self.decoder.decode(&packet) {
                Ok(d) => d,
                Err(SymError::DecodeError(_)) => {
                    self.errors += 1;
                    continue;
                }
                Err(e) => return Err(e.into()),
            };
            if self.info.channels == 0 {
                self.info.channels = decoded.spec().channels().count();
            }
            decoded.copy_to_vec_interleaved(out);
            return Ok(Some(first));
        }
    }

    fn decode_errors(&self) -> u64 {
        self.errors
    }
}
