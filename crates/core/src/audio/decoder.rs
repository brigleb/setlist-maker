//! symphonia 0.6 behind a small interface: decoded i16 chunks with their position, and seeking.
//!
//! Gapless trimming happens in the decoder in 0.6: a packet carries `pts` (negative for the
//! encoder delay) plus `trim_start`, so a decoded buffer's first frame is at
//! `pts + trim_start`.

use std::fs::File;
use std::path::Path;

use symphonia::core::codecs::CodecParameters;
use symphonia::core::codecs::audio::well_known::CODEC_ID_MP3;
use symphonia::core::codecs::audio::{AudioDecoder, AudioDecoderOptions};
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, FormatReader, SeekMode, SeekTo};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::units::Timestamp;

use super::{AudioError, PREROLL, mp3};

pub struct Decoder {
    format: Box<dyn FormatReader>,
    decoder: Box<dyn AudioDecoder>,
    track: u32,
    rate: u32,
    channels: usize,
    stated_frames: Option<u64>,
}

impl Decoder {
    pub fn open(path: &Path) -> Result<Decoder, AudioError> {
        let mss = MediaSourceStream::new(Box::new(File::open(path)?), Default::default());
        let mut hint = Hint::new();
        if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
            hint.with_extension(ext);
        }
        let format = symphonia::default::get_probe().probe(
            &hint,
            mss,
            FormatOptions::default(),
            MetadataOptions::default(),
        )?;
        let track = format
            .tracks()
            .iter()
            .find(|t| matches!(t.codec_params, Some(CodecParameters::Audio(_))))
            .ok_or_else(|| AudioError::Unreadable("no audio track".into()))?
            .clone();
        let Some(CodecParameters::Audio(params)) = &track.codec_params else {
            unreachable!("found by its audio parameters")
        };
        let rate = params
            .sample_rate
            .ok_or_else(|| AudioError::Unreadable("unknown sample rate".into()))?;
        // symphonia always has a length for an MP3, but without a Xing, Info or VBRI frame it
        // is estimated from the file's size, which shrinks with a cut file, and is far off for
        // VBR (a good 2019 set estimated at 19235s decodes 12013s). Only a stated length can
        // judge the decode.
        let stated_frames = track
            .num_frames
            .filter(|_| params.codec != CODEC_ID_MP3 || mp3::states_length(path));
        let decoder = symphonia::default::get_codecs()
            .make_audio_decoder(params, &AudioDecoderOptions::default())?;
        Ok(Decoder {
            format,
            decoder,
            track: track.id,
            rate,
            channels: params.channels.as_ref().map(|c| c.count()).unwrap_or(0),
            stated_frames,
        })
    }

    pub fn rate(&self) -> u32 {
        self.rate
    }

    /// The length the container states: a WAV, FLAC or MP4 header, or an MP3's Xing, Info or
    /// VBRI frame. `None` when it only estimates one.
    pub fn stated_frames(&self) -> Option<u64> {
        self.stated_frames
    }

    /// Accurate seek, so the next chunk holds `frame` (or starts before it).
    fn seek(&mut self, frame: u64) -> Result<(), AudioError> {
        let to = SeekTo::Timestamp {
            ts: Timestamp::new(frame as i64),
            track_id: self.track,
        };
        self.format.seek(SeekMode::Accurate, to)?;
        self.decoder.reset();
        Ok(())
    }

    /// The next decoded chunk, appended to `out` interleaved, with its first frame's position.
    /// `None` at the end. A packet that fails to decode is skipped, as rodio skips it.
    fn next_chunk(&mut self, out: &mut Vec<i16>) -> Result<Option<(i64, usize)>, AudioError> {
        loop {
            let packet = match self.format.next_packet() {
                Ok(Some(p)) => p,
                Ok(None) => return Ok(None),
                Err(SymError::IoError(e)) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                    return Ok(None);
                }
                Err(e) => return Err(e.into()),
            };
            if packet.track_id != self.track {
                continue;
            }
            let first = packet.pts.get() + packet.trim_start.get() as i64;
            let decoded = match self.decoder.decode(&packet) {
                Ok(d) => d,
                Err(SymError::DecodeError(_)) => continue,
                Err(e) => return Err(e.into()),
            };
            if self.channels == 0 {
                self.channels = decoded.spec().channels().count();
            }
            decoded.copy_to_vec_interleaved(out);
            return Ok(Some((first, decoded.frames())));
        }
    }

    /// How many frames decode from here to the end.
    pub fn count_remaining_frames(&mut self) -> Result<u64, AudioError> {
        let mut buf = Vec::new();
        let mut frames = 0;
        while let Some((_, n)) = self.next_chunk(&mut buf)? {
            frames += n as u64;
            buf.clear();
        }
        Ok(frames)
    }

    /// `count` frames of the first channel from `first`. With `seek`, starts [`PREROLL`]
    /// frames early and discards them; without, decodes from the top (to check the seek).
    pub fn first_channel(
        &mut self,
        first: u64,
        count: usize,
        seek: bool,
    ) -> Result<Vec<i16>, AudioError> {
        if seek && first > 0 {
            self.seek(first.saturating_sub(PREROLL))?;
        }
        let mut out = Vec::with_capacity(count);
        let mut buf = Vec::new();
        while out.len() < count {
            buf.clear();
            let Some((at, _)) = self.next_chunk(&mut buf)? else {
                break;
            };
            let ch = self.channels.max(1);
            let skip = (first as i64 - at).max(0) as usize;
            out.extend(
                buf.chunks(ch)
                    .skip(skip)
                    .map(|frame| frame[0])
                    .take(count - out.len()),
            );
        }
        Ok(out)
    }
}
