//! Whole-engine properties, ported from Python's `test_boundary_properties.py`: correctness,
//! cost, the anytime property and measured precision, against the synthetic oracle.
//!
//! The oracle draws Python's random numbers, so each test also pins the exact result Python
//! measured on the same seed (probe counts, the p90 error, 43 impostors of 89). A difference
//! there means the port folds differently, even where the bound still holds. If the measured
//! precision gate fails, the defaults are wrong, not the test.

use std::collections::BTreeSet;

use super::boundary::{BoundaryEngine, EngineConfig};
use super::oracle::{
    SyntheticSet, SyntheticTrack, assert_identities_distinct, run_engine, synthetic_identity,
};
use super::pyrandom::Random;
use crate::model::Probe;

fn random_set(seed: u64, duration: f64, n_tracks: usize) -> SyntheticSet {
    let cut_in_rate = 0.5;
    let mut rng = Random::new(seed);
    let mut starts: Vec<f64> = (0..n_tracks - 1)
        .map(|_| rng.uniform(120.0, duration - 240.0))
        .collect();
    starts.sort_by(f64::total_cmp);
    // Tracks are at least 2 minutes apart.
    let (mut keep, mut last) = (vec![0.0], 0.0);
    for s in starts {
        if s - last >= 120.0 {
            keep.push(s);
            last = s;
        }
    }
    let tracks: Vec<SyntheticTrack> = keep
        .into_iter()
        .enumerate()
        .map(|(i, start)| {
            let (artist, title) = synthetic_identity(i);
            let cut_in = if rng.random() < cut_in_rate {
                rng.uniform(0.0, 90.0)
            } else {
                0.0
            };
            SyntheticTrack {
                cut_in,
                ..SyntheticTrack::new(&artist, &title, start)
            }
        })
        .collect();
    assert_identities_distinct(&tracks);
    // The clean-oracle cases answer by majority; the measurement gates turn blur back on.
    let mut set = SyntheticSet::new(duration, tracks, seed);
    set.edge_blur = false;
    set
}

fn engine(duration: f64) -> BoundaryEngine {
    BoundaryEngine::new(duration, EngineConfig::default())
}

fn boundary_errors(engine: &BoundaryEngine, oracle: &SyntheticSet) -> Vec<f64> {
    let starts: Vec<f64> = engine.segments().0.iter().map(|s| s.start).collect();
    oracle
        .boundaries()
        .iter()
        .map(|b| {
            starts
                .iter()
                .map(|s| (b - s).abs())
                .fold(f64::INFINITY, f64::min)
        })
        .collect()
}

fn titles(engine: &BoundaryEngine) -> BTreeSet<String> {
    engine
        .segments()
        .0
        .iter()
        .filter_map(|s| {
            s.info
                .as_ref()
                .map(|i| i["title"].as_str().unwrap().to_string())
        })
        .collect()
}

#[test]
fn clean_oracle_finds_every_boundary_within_precision() {
    let mut oracle = random_set(1, 14400.0, 40);
    let mut eng = engine(oracle.duration);
    let n = run_engine(&mut eng, &mut oracle, 2000);
    let worst = boundary_errors(&eng, &oracle)
        .into_iter()
        .fold(0.0, f64::max);
    assert!(worst <= eng.config().precision);
    // Cost: coverage (~160) plus a few refines per boundary.
    assert!(n <= 160 + 6 * oracle.boundaries().len());
    assert_eq!(
        (n, oracle.boundaries().len(), worst),
        (220, 26, 3.9138561320869485)
    );
}

#[test]
fn no_track_of_at_least_two_minutes_is_missed() {
    let mut oracle = random_set(2, 14400.0, 40);
    let mut eng = engine(oracle.duration);
    let n = run_engine(&mut eng, &mut oracle, 2000);
    let real: BTreeSet<String> = oracle.tracks.iter().map(|t| t.title.clone()).collect();
    assert_eq!(titles(&eng), real);
    assert_eq!((n, real.len()), (241, 31));
}

#[test]
fn anytime_prefix_always_yields_complete_tracklist() {
    let mut oracle = random_set(3, 3600.0, 12);
    let mut eng = engine(oracle.duration);
    let mut worst = Vec::new();
    while let Some(plan) = eng.next_probe() {
        assert!(worst.len() < 2000);
        eng.add_probe(oracle.probe_for(plan));
        let segs = eng.segments().0;
        assert_eq!(segs[0].start, 0.0); // complete at every prefix
        worst.push(
            boundary_errors(&eng, &oracle)
                .into_iter()
                .fold(0.0, f64::max),
        );
    }
    // Uncertainty trends down: the final read beats one a quarter of the way in.
    let (last, quarter) = (worst[worst.len() - 1], worst[worst.len() / 4]);
    assert!(last <= quarter);
    assert_eq!(
        (worst.len(), last, quarter),
        (59, 2.0556173597616407, 1269.188342002229)
    );
}

#[test]
fn replay_equality() {
    let mut oracle = random_set(4, 3600.0, 10);
    let mut eng = engine(oracle.duration);
    run_engine(&mut eng, &mut oracle, 2000);
    let mut replayed = engine(oracle.duration);
    for p in eng.probes() {
        replayed.add_probe(Probe::clone(p));
    }
    assert_eq!(replayed.next_probe(), None);
    assert_eq!(replayed.segments(), eng.segments());
}

#[test]
fn measured_precision_supports_defaults() {
    // The measurement gate: offset jitter, 30% offset dropout, blurred boundary windows.
    // p90 of the boundary error must stay within the default precision.
    let mut errors = Vec::new();
    let mut counts = Vec::new();
    for seed in 0..5 {
        let mut oracle = random_set(100 + seed, 7200.0, 20);
        oracle.offset_jitter = 1.0;
        oracle.offset_dropout = 0.3;
        oracle.edge_blur = true;
        let mut eng = engine(oracle.duration);
        counts.push(run_engine(&mut eng, &mut oracle, 2000));
        errors.extend(boundary_errors(&eng, &oracle));
    }
    errors.sort_by(f64::total_cmp);
    let p90 = errors[errors.len() * 9 / 10];
    assert!(p90 <= EngineConfig::default().precision, "p90 {p90}");
    assert_eq!(counts, [122, 136, 122, 118, 125]);
    assert_eq!((errors.len(), p90), (71, 4.144984684561678));
}

#[test]
fn gap_heavy_set_converges_with_unidentified_segments() {
    let tracks = vec![
        SyntheticTrack::new("A", "A", 0.0),
        SyntheticTrack::new("B", "B", 1200.0),
    ];
    let mut oracle = SyntheticSet::new(1800.0, tracks, 5);
    oracle.gaps = vec![(500.0, 900.0)];
    let mut eng = engine(oracle.duration);
    let n = run_engine(&mut eng, &mut oracle, 2000);
    let segs: Vec<(f64, Option<String>)> = eng
        .segments()
        .0
        .into_iter()
        .map(|s| {
            (
                s.start,
                s.info.map(|i| i["title"].as_str().unwrap().to_string()),
            )
        })
        .collect();
    assert!(segs.iter().any(|s| s.1.is_none()));
    assert!(n < 200);
    assert_eq!(n, 34);
    let a = || Some("A".to_string());
    assert_eq!(
        segs,
        [
            (0.0, a()),
            (506.25, None),
            (888.75, a()),
            (1200.0, Some("B".into()))
        ]
    );
}

#[test]
fn misidentified_phantoms_collapse_without_taking_a_real_track() {
    // The gate for contradiction collapse: misid_rate supplies the one failure the plain
    // oracle can't, a record answering over the audio it samples. The rule may erase
    // impostors; it may not erase a real track the probes found.
    let (mut erased, mut left, mut seen) = (0, 0, 0);
    let mut counts = Vec::new();
    for seed in 0..12 {
        let mut oracle = random_set(700 + seed, 7200.0, 14);
        oracle.edge_blur = true;
        oracle.misid_rate = 0.12;
        let mut eng = engine(oracle.duration);
        counts.push(run_engine(&mut eng, &mut oracle, 2000));
        let kept = titles(&eng);
        let named: BTreeSet<String> = eng
            .probes()
            .iter()
            .filter_map(|p| {
                p.result
                    .as_ref()
                    .map(|r| r["title"].as_str().unwrap().to_string())
            })
            .collect();
        let real: BTreeSet<String> = oracle.tracks.iter().map(|t| t.title.clone()).collect();
        let found: BTreeSet<&String> = real.intersection(&named).collect();
        erased += found.iter().filter(|t| !kept.contains(**t)).count();
        let phantoms: BTreeSet<&String> = named.difference(&real).collect();
        seen += phantoms.len();
        left += phantoms.iter().filter(|t| kept.contains(**t)).count();
    }
    assert!(seen > 0, "the fixture produced no misidentifications");
    assert_eq!(erased, 0, "real tracks the probes found were erased");
    // Most survivors land alone inside a long track, where A-B-A belongs to the confidence
    // rule; the committed fixtures, not this gate, pin efficacy.
    assert!(left as f64 <= 0.7 * seen as f64);
    assert_eq!((left, seen), (43, 89));
    assert_eq!(
        counts,
        [168, 195, 166, 200, 159, 184, 214, 211, 199, 176, 214, 200]
    );
}
