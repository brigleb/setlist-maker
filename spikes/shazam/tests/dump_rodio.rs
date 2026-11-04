//! Writes rodio 0.20.1's resampling of a short deterministic input, as raw i16 LE, for
//! hindsight-core's resampler test (which then needs no rodio). Run once:
//!   cargo test --test dump_rodio -- --ignored
use rodio::buffer::SamplesBuffer;
use rodio::source::UniformSourceIterator;

fn input(n: usize) -> Vec<i16> {
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
#[ignore]
fn dump() {
    let out = std::env::var("DUMP_DIR").unwrap();
    for from in [44100u32, 48000] {
        let pcm = input(from as usize / 5);
        let theirs: Vec<i16> =
            UniformSourceIterator::<_, i16>::new(SamplesBuffer::new(1, from, pcm), 1, 16000)
                .collect();
        let bytes: Vec<u8> = theirs.iter().flat_map(|s| s.to_le_bytes()).collect();
        std::fs::write(format!("{out}/rodio_{from}_to_16000.i16"), bytes).unwrap();
    }
}
