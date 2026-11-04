//! The signature must be byte-identical to shazamio-core 1.1.2's for the same
//! 16 kHz mono PCM. The fixture was produced by shazamio-core itself
//! (`make_fixture.py`), from the same deterministic 12s of audio regenerated here.

const RATE: usize = 16000;
const SECONDS: usize = 12;
const PERIODS: [usize; 5] = [37, 23, 11, 61, 17];

/// Same integer-only generator as make_fixture.py's `test_pcm`.
fn test_pcm() -> Vec<i16> {
    let mut state: u64 = 1;
    (0..RATE * SECONDS)
        .map(|i| {
            state = (state * 1103515245 + 12345) & 0x7FFF_FFFF;
            let noise = ((state >> 16) % 4001) as i64 - 2000;
            let period = PERIODS[(i / 8000) % PERIODS.len()];
            let square = if i % period < period / 2 { 6000 } else { -6000 };
            (noise + square).clamp(-32768, 32767) as i16
        })
        .collect()
}

#[test]
fn signature_matches_shazamio_core_for_the_same_pcm() {
    let expected = include_str!("fixtures/lcg_12s.uri").trim();
    assert_eq!(shazam_spike::signature::fingerprint(&test_pcm()).uri, expected);
}
