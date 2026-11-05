//! The fixtures are made with ffmpeg (`fixtures/README.md`): a CBR MP3 with no Xing frame,
//! like Ray's Logic exports; a 44.1 kHz MP3 with an Info frame; and a FLAC.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use super::*;

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("src/audio/fixtures")
        .join(name)
}

/// A copy in a temp dir, with `edit` applied to its bytes.
fn edited(name: &str, edit: impl FnOnce(&mut Vec<u8>)) -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join(name);
    let mut bytes = fs::read(fixture(name)).unwrap();
    edit(&mut bytes);
    fs::write(&path, bytes).unwrap();
    (dir, path)
}

/// 10s of 8 kHz mono 16-bit WAV: a header that states the length, then a ramp.
fn wav_bytes() -> Vec<u8> {
    let samples: Vec<i16> = (0..80_000)
        .map(|i| ((i * 37) % 20_000 - 10_000) as i16)
        .collect();
    let data = samples.len() as u32 * 2;
    let mut b = Vec::new();
    b.extend_from_slice(b"RIFF");
    b.extend_from_slice(&(36 + data).to_le_bytes());
    b.extend_from_slice(b"WAVEfmt ");
    b.extend_from_slice(&16u32.to_le_bytes()); // fmt chunk size
    b.extend_from_slice(&1u16.to_le_bytes()); // PCM
    b.extend_from_slice(&1u16.to_le_bytes()); // mono
    b.extend_from_slice(&8000u32.to_le_bytes());
    b.extend_from_slice(&16000u32.to_le_bytes());
    b.extend_from_slice(&2u16.to_le_bytes());
    b.extend_from_slice(&16u16.to_le_bytes());
    b.extend_from_slice(b"data");
    b.extend_from_slice(&data.to_le_bytes());
    for s in samples {
        b.extend_from_slice(&s.to_le_bytes());
    }
    b
}

fn rodio_input(n: usize) -> Vec<i16> {
    let mut state: u64 = 7;
    (0..n)
        .map(|_| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (((state >> 40) % 60001) as i64 - 30000) as i16
        })
        .collect()
}

#[test]
fn resampling_matches_rodio() {
    // rodio 0.20.1's output for the same input, dumped by spikes/shazam/tests/dump_rodio.rs.
    // 44.1 kHz exercises the interpolation; 48 kHz, the ground-truth set's rate, is an exact
    // 3:1 that only drops samples.
    for (from, theirs) in [
        (44100, &include_bytes!("rodio_44100_to_16000.i16")[..]),
        (48000, &include_bytes!("rodio_48000_to_16000.i16")[..]),
    ] {
        let theirs: Vec<i16> = theirs
            .chunks(2)
            .map(|b| i16::from_le_bytes([b[0], b[1]]))
            .collect();
        let ours = resample_linear(&rodio_input(from as usize / 5), from, 16000);
        let n = ours.len().min(theirs.len());
        assert!(n > 3000, "{from}: only {n} samples");
        assert_eq!(ours[..n], theirs[..n], "{from}");
        assert!(ours.len().abs_diff(theirs.len()) <= 1, "{from}");
    }
}

#[test]
fn only_a_stated_length_is_trusted() {
    assert!(mp3::states_length(&fixture("mono44_info.mp3")));
    assert!(!mp3::states_length(&fixture("stereo48_cbr.mp3")));
    assert_eq!(
        Decoder::open(&fixture("stereo48_cbr.mp3"))
            .unwrap()
            .stated_frames(),
        None
    );
    let stated = |name: &str| Decoder::open(&fixture(name)).unwrap().stated_frames();
    assert_eq!(stated("mono44_info.mp3"), Some(441_000));
    assert_eq!(stated("tone8k.flac"), Some(80_000));
}

#[test]
fn a_vbri_frame_states_a_length_too() {
    // MPEG1 stereo header, then VBRI 32 bytes after it.
    let mut frame = vec![0xff, 0xfb, 0x90, 0x44];
    frame.resize(36, 0);
    frame.extend_from_slice(b"VBRI");
    assert!(mp3::first_frame_states_length(&frame));
    frame[36..40].copy_from_slice(b"VBRX");
    assert!(!mp3::first_frame_states_length(&frame));
}

#[test]
fn a_recording_is_measured_by_decoding_it() {
    let rec = Recording::open(&fixture("stereo48_cbr.mp3"), false).unwrap();
    assert_eq!(rec.rate(), 48000);
    // ffprobe says 12.024 (no delay to trim: no LAME tag).
    assert_eq!(rec.duration(), 12.024);
    assert_eq!(rec.stated_seconds(), None);
    let rec = Recording::open(&fixture("mono44_info.mp3"), false).unwrap();
    assert_eq!((rec.frames(), rec.duration()), (441_000, 10.0));
}

#[test]
fn a_recording_opened_without_decoding_has_the_same_windows() {
    for name in ["stereo48_cbr.mp3", "mono44_info.mp3"] {
        let decoded = Recording::open(&fixture(name), false).unwrap();
        let quick = Recording::open_undecoded(&fixture(name)).unwrap();
        assert_eq!(quick.frames(), decoded.frames(), "{name}");
        assert_eq!(
            quick.window_16k(2.0, 6.0).unwrap(),
            decoded.window_16k(2.0, 6.0).unwrap()
        );
    }
}

#[test]
fn the_packets_add_up_to_the_decoded_length() {
    for name in ["stereo48_cbr.mp3", "mono44_info.mp3", "tone8k.flac"] {
        let rec = Recording::open(&fixture(name), false).unwrap();
        let undecoded = undecoded_seconds(&fixture(name)).unwrap();
        assert_eq!(undecoded, rec.decoded_seconds(), "{name}");
    }
}

#[test]
fn seeked_windows_match_a_continuous_decode() {
    for (name, positions) in [
        ("stereo48_cbr.mp3", &[0.0, 0.37, 2.5, 5.0123, 9.9, 11.5][..]),
        ("mono44_info.mp3", &[0.01, 3.3, 7.77][..]),
        ("tone8k.flac", &[1.0, 6.5][..]),
    ] {
        let rec = Recording::open(&fixture(name), false).unwrap();
        for &t in positions {
            let first = (t * rec.rate() as f64).round() as u64;
            let count = (1.5 * rec.rate() as f64) as usize;
            let seeked = rec.window_first_channel(t, 1.5).unwrap();
            let continuous = Decoder::open(&fixture(name))
                .unwrap()
                .first_channel(first, count, false)
                .unwrap();
            assert_eq!(seeked.len(), continuous.len(), "{name} at {t}");
            assert!(seeked == continuous, "{name} at {t}: seeked window differs");
        }
    }
}

#[test]
fn a_window_near_the_end_comes_back_short() {
    let rec = Recording::open(&fixture("tone8k.flac"), false).unwrap();
    assert_eq!(rec.window_first_channel(9.0, 3.0).unwrap().len(), 8000);
    assert_eq!(rec.window_first_channel(12.0, 3.0).unwrap().len(), 0);
    assert_eq!(rec.window_16k(9.0, 3.0).unwrap().len(), 15_998);
}

#[test]
fn the_guard_trips_on_a_cut_or_zero_tailed_file() {
    let truncated = |path: &Path| {
        matches!(
            Recording::open(path, false),
            Err(AudioError::Truncated { .. })
        )
    };
    let cut = |b: &mut Vec<u8>| b.truncate(b.len() * 6 / 10);
    let (_d, flac) = edited("tone8k.flac", cut);
    assert!(truncated(&flac));
    // A file the OS reports at full size with only part of it there yet.
    let (_d, mp3) = edited("mono44_info.mp3", |b| {
        let from = b.len() * 6 / 10;
        b[from..].fill(0);
    });
    assert!(truncated(&mp3));
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("cut.wav");
    let mut bytes = wav_bytes();
    fs::write(dir.path().join("whole.wav"), &bytes).unwrap();
    cut(&mut bytes);
    fs::write(&wav, bytes).unwrap();
    assert!(truncated(&wav));
    // --allow-partial takes what decoded.
    let rec = Recording::open(&wav, true).unwrap();
    assert!((rec.decoded_seconds() - 6.0).abs() < 0.01);

    for good in [fixture("tone8k.flac"), fixture("mono44_info.mp3")] {
        assert!(Recording::open(&good, false).is_ok(), "{good:?}");
    }
    assert!(Recording::open(&dir.path().join("whole.wav"), false).is_ok());
}

#[test]
fn the_guard_skips_a_file_that_states_no_length() {
    // A zero-tailed CBR MP3 with no Xing frame (Ray's Logic exports have none). symphonia's
    // size-based estimate would catch this one, but the same estimate is far off for VBR and
    // can't see a cut file, so the plan trusts only stated lengths, and this goes unguarded.
    let (_d, mp3) = edited("stereo48_cbr.mp3", |b| {
        let from = b.len() * 6 / 10;
        b[from..].fill(0);
    });
    let rec = Recording::open(&mp3, false).unwrap();
    assert!(rec.decoded_seconds() < 8.0);
}

#[test]
fn guard_tolerances() {
    // Python's test_audio cases.
    let trips = |d, s| verify_decode_complete(d, s).is_err();
    assert!(!trips(15148.0, Some(15148.0)));
    assert!(!trips(4680.0, None));
    assert!(!trips(4680.0, Some(0.0)));
    assert!(!trips(7199.0, Some(7200.0)));
    assert!(!trips(7205.0, Some(7200.0)));
    assert!(trips(4680.0, Some(15148.0)));
    let msg = verify_decode_complete(4680.0, Some(15148.0))
        .unwrap_err()
        .to_string();
    assert!(msg.contains("1:18:00") && msg.contains("4:12:28"), "{msg}");
}

/// ffmpeg is what Python's windows came from. Needs ffmpeg; skipped without it unless `CI` is
/// set, as Python's `requires_tool` tests are.
#[test]
fn windows_match_ffmpeg_within_two_lsb() {
    if Command::new("ffmpeg").arg("-version").output().is_err() {
        assert!(std::env::var_os("CI").is_none(), "ffmpeg is required in CI");
        eprintln!("skipped: no ffmpeg");
        return;
    }
    let path = fixture("stereo48_cbr.mp3");
    let rec = Recording::open(&path, false).unwrap();
    for t in [2.5, 4.0, 7.25, 9.0] {
        let ours = rec.window_first_channel(t, 2.0).unwrap();
        // ffmpeg's own -ss warms up the same way, so it starts 2s early too.
        let out = Command::new("ffmpeg")
            .args(["-v", "error", "-ss"])
            .arg(format!("{}", t - 2.0))
            .arg("-i")
            .arg(&path)
            .args(["-t", "4", "-af", "pan=mono|c0=c0", "-f", "s16le", "-"])
            .output()
            .unwrap();
        let theirs: Vec<i16> = out
            .stdout
            .chunks(2)
            .map(|b| i16::from_le_bytes([b[0], b[1]]))
            .skip(2 * 48000)
            .collect();
        let shift = (-200i64..=200)
            .min_by_key(|&s| {
                (2000..6000)
                    .map(|i| {
                        let j = (i as i64 + s) as usize;
                        (i64::from(ours[i]) - i64::from(theirs[j])).abs()
                    })
                    .sum::<i64>()
            })
            .unwrap();
        assert_eq!(shift, 0, "at {t}");
        let worst = ours
            .iter()
            .zip(&theirs)
            .map(|(a, b)| (i32::from(*a) - i32::from(*b)).abs())
            .max()
            .unwrap();
        assert!(worst <= 2, "at {t}: {worst} LSB");
    }
}
