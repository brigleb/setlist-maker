//! M0 spike 1 driver.
//!
//!   shazam-spike sample <audio> <start_s> <window_s>  one Shazam request, one JSON line
//!   shazam-spike seekcheck <audio> <start_s> <window_s>  seeked vs from-the-top PCM
//!
//! The excerpt Shazam hears starts `(window - 10) / 2` after the window does, so a match
//! at `offset` implies the track started at `start + (window - 10) / 2 - offset`.

use serde_json::json;
use shazam_spike::{decode, shazam, signature};
use std::path::Path;
use std::time::Instant;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 5 {
        eprintln!("usage: shazam-spike sample|seekcheck <audio> <start_s> <window_s>");
        std::process::exit(2);
    }
    let path = Path::new(&args[2]);
    let start: f64 = args[3].parse()?;
    let window: f64 = args[4].parse()?;

    match args[1].as_str() {
        "sample" => {
            let t = Instant::now();
            let pcm = decode::window_16k_mono(path, start, window, true)?;
            let decode_ms = t.elapsed().as_millis();
            let fp = signature::fingerprint(&pcm);
            let lead = fp.lead_s;
            let a = shazam::recognize(&fp.uri, fp.excerpt_ms)?;
            let implied: Vec<f64> = a.offsets.iter().map(|o| start + lead - o).collect();
            println!(
                "{}",
                json!({
                    "start": start, "window": window, "lead": lead,
                    "samples_16k": pcm.len(), "decode_ms": decode_ms,
                    "status": a.status, "artist": a.artist, "title": a.title,
                    "offsets": a.offsets, "implied": implied,
                })
            );
        }
        "seekcheck" => {
            let t = Instant::now();
            let (rate, seeked) = decode::window_first_channel(path, start, window, true)?;
            let seek_ms = t.elapsed().as_millis();
            let t = Instant::now();
            let (_, full) = decode::window_first_channel(path, start, window, false)?;
            let full_ms = t.elapsed().as_millis();
            // Find the shift that best aligns the seeked window with the true one.
            let shift = (-2000i64..=2000)
                .min_by_key(|&s| {
                    (0..4000usize)
                        .map(|i| {
                            let j = i as i64 + 2000 + s;
                            (seeked[i + 2000] as i64 - full[j as usize] as i64).abs()
                        })
                        .sum::<i64>()
                })
                .unwrap();
            let identical = seeked == full;
            let diffs: Vec<usize> = (0..seeked.len().min(full.len()))
                .filter(|&i| seeked[i] != full[i])
                .collect();
            let max_err = diffs
                .iter()
                .map(|&i| (seeked[i] as i32 - full[i] as i32).abs())
                .max()
                .unwrap_or(0);
            eprintln!(
                "differing samples: {} (first {:?}, last {:?}), max |err| {}",
                diffs.len(),
                diffs.first(),
                diffs.last(),
                max_err
            );
            println!(
                "{}",
                json!({
                    "rate": rate, "seek_ms": seek_ms, "from_top_ms": full_ms,
                    "identical": identical, "best_shift_samples": shift,
                    "len": [seeked.len(), full.len()],
                })
            );
        }
        other => return Err(format!("unknown command {other}").into()),
    }
    Ok(())
}
