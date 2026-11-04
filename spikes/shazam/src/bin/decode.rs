//! M0 spike 2 driver: symphonia decode, seek and duration, on 0.5 and 0.6.
//!
//!   decode info <file>                          what each version reports before decoding
//!   decode full <v> <file>                      one continuous decode: time, frames, hash
//!   decode sweep <v> <file> <win_s> <prerolls> <pos_s>...
//!                                               seeked windows vs the continuous decode
//!   decode parallel <v> <file> <n>              n seeked segments on n threads vs serial
//!   decode vdiff <file>                         0.5 and 0.6 in lockstep, every sample
//!   decode ffcmp <v> <file> <win_s> <pos_s>...  a seeked window vs ffmpeg's decode
//!
//! `<v>` is `05` or `06`; `<prerolls>` is a comma list of frame counts, e.g. `0,1152,4608`.
//! One JSON object per line on stdout.

use serde_json::json;
use shazam_spike::source::{self, diff, Error, Fnv, Version, PREROLL};
use std::collections::VecDeque;
use std::path::Path;
use std::process::Command;
use std::time::Instant;

fn version(s: &str) -> Version {
    match s {
        "05" => Version::V05,
        "06" => Version::V06,
        _ => panic!("version is 05 or 06"),
    }
}

fn main() -> Result<(), Error> {
    let a: Vec<String> = std::env::args().collect();
    match a.get(1).map(String::as_str) {
        Some("info") => info(Path::new(&a[2])),
        Some("full") => full(version(&a[2]), Path::new(&a[3])),
        Some("sweep") => {
            let prerolls = a[5].split(',').map(|p| p.parse().unwrap()).collect::<Vec<u64>>();
            let pos = a[6..].iter().map(|p| p.parse().unwrap()).collect::<Vec<f64>>();
            sweep(version(&a[2]), Path::new(&a[3]), a[4].parse()?, &prerolls, &pos)
        }
        Some("parallel") => parallel(version(&a[2]), Path::new(&a[3]), a[4].parse()?),
        Some("vdiff") => vdiff(Path::new(&a[2])),
        Some("ffcmp") => {
            let pos = a[5..].iter().map(|p| p.parse().unwrap()).collect::<Vec<f64>>();
            ffcmp(version(&a[2]), Path::new(&a[3]), a[4].parse()?, &pos)
        }
        _ => {
            eprintln!("usage: see the module doc");
            std::process::exit(2)
        }
    }
}

fn info(path: &Path) -> Result<(), Error> {
    for v in [Version::V05, Version::V06] {
        let t = Instant::now();
        let r = source::open(v, path).map(|s| s.info().clone());
        let ms = t.elapsed().as_secs_f64() * 1000.0;
        match r {
            Ok(i) => println!("{}", json!({"v": format!("{v:?}"), "open_ms": ms, "info": i})),
            Err(e) => println!("{}", json!({"v": format!("{v:?}"), "error": e.to_string()})),
        }
    }
    Ok(())
}

/// The whole file, front to back: the decode guard's "decoded" side.
fn full(v: Version, path: &Path) -> Result<(), Error> {
    let t = Instant::now();
    let mut src = source::open(v, path)?;
    let open_ms = t.elapsed().as_secs_f64() * 1000.0;
    let ch = src.info().channels;
    let (mut frames, mut hash, mut buf) = (0u64, Fnv::default(), Vec::new());
    let (mut first_ts, mut expected, mut gaps) = (None, 0i64, 0u64);
    while let Some(ts) = src.next_chunk(&mut buf)? {
        if first_ts.is_none() {
            first_ts = Some(ts);
            expected = ts;
        }
        if ts != expected {
            gaps += 1;
        }
        let n = (buf.len() / ch) as u64;
        expected = ts + n as i64;
        frames += n;
        hash.add(&buf);
    }
    let i = src.info();
    let decoded_s = frames as f64 / i.rate as f64;
    println!(
        "{}",
        json!({
            "v": format!("{v:?}"), "open_ms": open_ms,
            "wall_ms": t.elapsed().as_secs_f64() * 1000.0,
            "frames": frames, "decoded_s": decoded_s, "reported_s": i.reported_s,
            "n_frames": i.n_frames, "frames_minus_n_frames": i.n_frames.map(|n| frames as i64 - n as i64),
            "first_ts": first_ts, "ts_gaps": gaps, "decode_errors": src.decode_errors(),
            "guard_trips": guard_trips(decoded_s, i.reported_s),
            "hash": format!("{:016x}", hash.0),
        })
    );
    Ok(())
}

/// Python's `verify_decode_complete`: short by more than 2s *and* by more than 2%.
fn guard_trips(decoded: f64, reported: Option<f64>) -> Option<bool> {
    let r = reported.filter(|r| *r > 0.0)?;
    Some(r - decoded > 2.0 && decoded < r * 0.98)
}

/// One continuous decode captures every window; then each is decoded again by seeking, once
/// per preroll, and compared sample for sample (all channels).
fn sweep(v: Version, path: &Path, win: f64, prerolls: &[u64], pos: &[f64]) -> Result<(), Error> {
    let mut src = source::open(v, path)?;
    let (rate, ch) = (src.info().rate as f64, src.info().channels);
    let count = (win * rate).round() as usize;
    let mut wins: Vec<(u64, Vec<i16>)> =
        pos.iter().map(|p| ((p * rate).round() as u64, Vec::with_capacity(count * ch))).collect();
    wins.sort_by_key(|w| w.0);
    let t = Instant::now();
    let mut buf = Vec::new();
    while let Some(ts) = src.next_chunk(&mut buf)? {
        let end = ts + (buf.len() / ch) as i64;
        for (first, w) in wins.iter_mut() {
            let (f, l) = (*first as i64, *first as i64 + count as i64);
            if end <= f || ts >= l {
                continue;
            }
            let lo = (f.max(ts) - ts) as usize;
            let hi = (l.min(end) - ts) as usize;
            w.extend_from_slice(&buf[lo * ch..hi * ch]);
        }
    }
    eprintln!("continuous decode {:.0}ms", t.elapsed().as_secs_f64() * 1000.0);
    for (first, truth) in &wins {
        for &pre in prerolls {
            let t = Instant::now();
            let mut s = source::open(v, path)?;
            let got = source::window(s.as_mut(), *first, count, true, pre)?;
            let ms = t.elapsed().as_secs_f64() * 1000.0;
            let d = diff(&got, truth);
            let shift = (got != *truth).then(|| best_shift(&got, truth, ch, 8192));
            println!(
                "{}",
                json!({"v": format!("{v:?}"), "pos_s": *first as f64 / rate, "win_s": win,
                       "preroll": pre, "seek_ms": ms, "identical": got == *truth,
                       "shift_frames": shift,
                       "diff_frames": d.differing.div_ceil(ch.max(1)),
                       "first_diff_frame": d.first.map(|i| i / ch), "last_diff_frame": d.last.map(|i| i / ch),
                       "max_abs": d.max_abs, "len": d.len})
            );
        }
    }
    Ok(())
}

/// The shift `s` (frames) minimising |got[i] - truth[i + s]| on channel 0 over 4800 frames
/// from the middle: how far a seeked window sits from where it should.
fn best_shift(got: &[i16], truth: &[i16], ch: usize, reach: i64) -> i64 {
    let (a, b): (Vec<i16>, Vec<i16>) =
        (got.iter().step_by(ch).copied().collect(), truth.iter().step_by(ch).copied().collect());
    let (mid, span) = (a.len() / 2, 4800usize.min(a.len() / 4));
    (-reach..=reach)
        .min_by_key(|&s| {
            (0..span)
                .map(|i| {
                    let j = (mid + i) as i64 + s;
                    if j < 0 || j as usize >= b.len() { 1 << 16 } else { (a[mid + i] as i64 - b[j as usize] as i64).abs() }
                })
                .sum::<i64>()
        })
        .unwrap()
}

/// Peaks at 50/s (min, max of channel 0), as `peaks.py` cuts the work: n segments of whole
/// seconds, each seeked and decoded on its own thread, against one serial pass.
fn parallel(v: Version, path: &Path, n: u64) -> Result<(), Error> {
    const BIN: u64 = 50;
    let info = source::open(v, path)?.info().clone();
    let rate = info.rate as u64;
    let per_bin = rate / BIN;
    let total = info.n_frames.ok_or("no n_frames to split by")?;
    let secs = total.div_ceil(rate);
    let t = Instant::now();
    let serial = peaks(v, path, 0, secs * rate, per_bin)?;
    let serial_ms = t.elapsed().as_secs_f64() * 1000.0;
    let t = Instant::now();
    let bounds: Vec<(u64, u64)> = (0..n).map(|i| (secs * i / n * rate, secs * (i + 1) / n * rate)).collect();
    let parts: Vec<Vec<(i16, i16)>> = std::thread::scope(|sc| {
        let hs: Vec<_> = bounds.iter().map(|&(a, b)| sc.spawn(move || peaks(v, path, a, b, per_bin).unwrap())).collect();
        hs.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let par_ms = t.elapsed().as_secs_f64() * 1000.0;
    let par: Vec<(i16, i16)> = parts.into_iter().flatten().collect();
    let differing: Vec<usize> = (0..serial.len().min(par.len())).filter(|&i| serial[i] != par[i]).collect();
    println!(
        "{}",
        json!({"v": format!("{v:?}"), "segments": n, "serial_ms": serial_ms, "parallel_ms": par_ms,
               "bins": [serial.len(), par.len()], "differing_bins": differing.len(),
               "differing_at_s": differing.iter().take(20).map(|&i| i as f64 / BIN as f64).collect::<Vec<_>>()})
    );
    Ok(())
}

/// (min, max) of channel 0 per `per_bin` frames over `[a, b)`.
fn peaks(v: Version, path: &Path, a: u64, b: u64, per_bin: u64) -> Result<Vec<(i16, i16)>, Error> {
    let mut src = source::open(v, path)?;
    let ch = src.info().channels;
    if a > 0 {
        src.seek(a.saturating_sub(PREROLL))?; // discarded below
    }
    let nbins = (b - a).div_ceil(per_bin) as usize;
    let mut out = vec![(i16::MAX, i16::MIN); nbins];
    let mut buf = Vec::new();
    'outer: while let Some(ts) = src.next_chunk(&mut buf)? {
        for (i, f) in buf.chunks(ch).enumerate() {
            let at = ts + i as i64;
            if at < a as i64 {
                continue;
            }
            if at >= b as i64 {
                break 'outer;
            }
            let bin = &mut out[((at as u64 - a) / per_bin) as usize];
            bin.0 = bin.0.min(f[0]);
            bin.1 = bin.1.max(f[0]);
        }
    }
    Ok(out)
}

/// Both versions decode the whole file side by side; every sample is compared.
fn vdiff(path: &Path) -> Result<(), Error> {
    let mut s5 = source::open(Version::V05, path)?;
    let mut s6 = source::open(Version::V06, path)?;
    let ch = s5.info().channels;
    let (mut q5, mut q6) = (Pending::default(), Pending::default());
    let (mut compared, mut differing, mut max_abs) = (0u64, 0u64, 0i32);
    let mut first_diff: Option<i64> = None;
    let mut misaligned = 0u64;
    let mut buf = Vec::new();
    loop {
        if q5.is_empty(ch) && !q5.done {
            match s5.next_chunk(&mut buf)? {
                Some(ts) => q5.push(ts, &buf),
                None => q5.done = true,
            }
        }
        if q6.is_empty(ch) && !q6.done {
            match s6.next_chunk(&mut buf)? {
                Some(ts) => q6.push(ts, &buf),
                None => q6.done = true,
            }
        }
        if q5.is_empty(ch) || q6.is_empty(ch) {
            if (q5.is_empty(ch) && q5.done) || (q6.is_empty(ch) && q6.done) {
                break;
            }
            continue;
        }
        if q5.ts != q6.ts {
            misaligned += 1;
            // Drop the earlier side's head until they line up.
            if q5.ts < q6.ts { q5.pop(ch); } else { q6.pop(ch); }
            continue;
        }
        let (a, b) = (q5.pop(ch), q6.pop(ch));
        for (x, y) in a.iter().zip(&b) {
            if x != y {
                differing += 1;
                max_abs = max_abs.max((*x as i32 - *y as i32).abs());
                first_diff.get_or_insert(q5.ts - 1);
            }
        }
        compared += 1;
    }
    let left = (q5.frames.len() / ch, q6.frames.len() / ch);
    println!(
        "{}",
        json!({"frames_compared": compared, "differing_samples": differing, "max_abs": max_abs,
               "first_diff_frame": first_diff, "misaligned_frames_dropped": misaligned,
               "trailing": [left.0, left.1], "ended": [q5.done, q6.done]})
    );
    Ok(())
}

#[derive(Default)]
struct Pending {
    ts: i64,
    frames: VecDeque<i16>,
    done: bool,
}

impl Pending {
    fn is_empty(&self, ch: usize) -> bool {
        self.frames.len() < ch
    }
    fn push(&mut self, ts: i64, buf: &[i16]) {
        if self.frames.is_empty() {
            self.ts = ts;
        }
        self.frames.extend(buf);
    }
    fn pop(&mut self, ch: usize) -> Vec<i16> {
        self.ts += 1;
        self.frames.drain(..ch).collect()
    }
}

/// A seeked window against ffmpeg's accurate `-ss` decode of the same span. Reports the
/// alignment (ffmpeg is what Python's windows came from) and the residual difference.
fn ffcmp(v: Version, path: &Path, win: f64, pos: &[f64]) -> Result<(), Error> {
    for &p in pos {
        let mut src = source::open(v, path)?;
        let (rate, ch) = (src.info().rate as f64, src.info().channels);
        let first = (p * rate).round() as u64;
        let count = (win * rate).round() as usize;
        let ours = source::window(src.as_mut(), first, count, true, PREROLL)?;
        let out = Command::new("ffmpeg")
            .args(["-nostdin", "-v", "error", "-ss", &format!("{p}"), "-i"])
            .arg(path)
            .args(["-t", &format!("{win}"), "-map", "0:a:0", "-f", "s16le", "-"])
            .output()?;
        let ff: Vec<i16> = out.stdout.chunks_exact(2).map(|b| i16::from_le_bytes([b[0], b[1]])).collect();
        let c0 = |x: &[i16]| x.iter().step_by(ch).copied().collect::<Vec<i16>>();
        let (a, b) = (c0(&ours), c0(&ff));
        // Best shift of ffmpeg against ours over 1s from the middle of the window.
        let (mid, span, reach) = (a.len() / 2, (rate as usize).min(a.len() / 4), 4096i64);
        let cost = |s: i64| -> i64 {
            (0..span)
                .map(|i| {
                    let j = (mid + i) as i64 + s;
                    if j < 0 || j as usize >= b.len() { 1 << 16 } else { (a[mid + i] as i64 - b[j as usize] as i64).abs() }
                })
                .sum()
        };
        let shift = (-reach..=reach).min_by_key(|&s| cost(s)).unwrap();
        let aligned: Vec<(i16, i16)> = (0..a.len())
            .filter_map(|i| {
                let j = i as i64 + shift;
                (j >= 0 && (j as usize) < b.len()).then(|| (a[i], b[j as usize]))
            })
            .collect();
        let errs: Vec<i32> = aligned.iter().map(|(x, y)| (*x as i32 - *y as i32).abs()).collect();
        let max = errs.iter().copied().max().unwrap_or(0);
        let mean = errs.iter().map(|&e| e as f64).sum::<f64>() / errs.len().max(1) as f64;
        let over2 = errs.iter().filter(|&&e| e > 2).count();
        println!(
            "{}",
            json!({"v": format!("{v:?}"), "pos_s": p, "win_s": win, "ffmpeg_frames": b.len(), "ours_frames": a.len(),
                   "shift_frames": shift, "shift_ms": shift as f64 / rate * 1000.0,
                   "max_abs": max, "mean_abs": mean, "samples_over_2lsb": over2})
        );
    }
    Ok(())
}
