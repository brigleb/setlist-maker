//! `resample_linear` must produce what rodio 0.20.1 (shazamio-core's resampler) produces.
//! A `SamplesBuffer` reports no frame length, so rodio resamples it continuously, which is
//! the behaviour `resample_linear` copies. 44.1 kHz exercises the interpolation (48 kHz,
//! the ground-truth set's rate, is an exact 3:1 and only ever drops samples).

use rodio::buffer::SamplesBuffer;
use rodio::source::UniformSourceIterator;
use shazam_spike::decode::resample_linear;

fn input() -> Vec<i16> {
    let mut state: u64 = 7;
    (0..44100 * 2)
        .map(|_| {
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (((state >> 40) % 60001) as i64 - 30000) as i16
        })
        .collect()
}

fn rodio_resample(pcm: &[i16], from: u32) -> Vec<i16> {
    let src = SamplesBuffer::new(1, from, pcm.to_vec());
    UniformSourceIterator::<_, i16>::new(src, 1, 16000).collect()
}

#[test]
fn matches_rodio_from_44100() {
    let pcm = input();
    let ours = resample_linear(&pcm, 44100, 16000);
    let theirs = rodio_resample(&pcm, 44100);
    let n = ours.len().min(theirs.len());
    assert!(n > 31000, "only {n} samples");
    assert_eq!(ours[..n], theirs[..n]);
    assert!(ours.len().abs_diff(theirs.len()) <= 1, "{} vs {}", ours.len(), theirs.len());
}

#[test]
fn matches_rodio_from_48000() {
    let pcm = input();
    let ours = resample_linear(&pcm, 48000, 16000);
    let theirs = rodio_resample(&pcm, 48000);
    let n = ours.len().min(theirs.len());
    assert_eq!(ours[..n], theirs[..n]);
    assert!(ours.len().abs_diff(theirs.len()) <= 1, "{} vs {}", ours.len(), theirs.len());
}
