//! Python's `test_boundary_engine.py` and `test_boundary_segments.py`, ported.

use std::path::Path;

use serde_json::{Value, json};

use super::*;
use crate::model::load_progress;

/// A probe: a 30s coverage probe of nothing unless a title is given.
struct P(Probe);

fn probe(t: f64) -> P {
    P(Probe {
        t,
        window: 30.0,
        purpose: "coverage".into(),
        result: None,
        offsets: None,
    })
}

impl P {
    fn title(mut self, title: &str) -> Self {
        self.0.result = Some(json!({"artist": "X", "title": title, "confidence": 0.9}));
        self
    }
    fn artist(mut self, artist: &str) -> Self {
        self.0.result.as_mut().unwrap()["artist"] = json!(artist);
        self
    }
    fn confidence(mut self, c: f64) -> Self {
        self.0.result.as_mut().unwrap()["confidence"] = json!(c);
        self
    }
    fn window(mut self, w: f64) -> Self {
        self.0.window = w;
        self
    }
    fn refine(mut self) -> Self {
        self.0.purpose = "refine".into();
        self
    }
    fn offset(self, o: f64) -> Self {
        self.skewed(o, 0.0)
    }
    fn skewed(mut self, o: f64, skew: f64) -> Self {
        self.0.offsets = Some(vec![json!({"offset": o, "timeskew": skew})]);
        self
    }
}

fn engine(duration: f64) -> BoundaryEngine {
    BoundaryEngine::new(duration, EngineConfig::default())
}

fn add(eng: &mut BoundaryEngine, p: P) -> Vec<Event> {
    eng.add_probe(p.0)
}

fn mids(points: &[Evidence]) -> Vec<f64> {
    points.iter().map(|p| p.mid).collect()
}

fn targets(eng: &BoundaryEngine) -> Vec<f64> {
    eng.pairs().map(|(l, r)| eng.target(l, r)).collect()
}

fn boundary(eng: &BoundaryEngine) -> (Evidence, Evidence) {
    eng.pairs().find(|&(l, r)| eng.is_boundary(l, r)).unwrap()
}

fn titles(segs: &[Segment]) -> Vec<Option<&str>> {
    segs.iter()
        .map(|s| s.info.as_ref().map(|i| i["title"].as_str().unwrap()))
        .collect()
}

fn named(segs: &[Segment]) -> Vec<&str> {
    titles(segs).into_iter().flatten().collect()
}

fn types(events: &[Event]) -> Vec<String> {
    events
        .iter()
        .map(|e| {
            serde_json::to_value(e).unwrap()["type"]
                .as_str()
                .unwrap()
                .to_string()
        })
        .collect()
}

fn as_json(drops: &[Dropped]) -> Vec<Value> {
    drops
        .iter()
        .map(|d| serde_json::to_value(d).unwrap())
        .collect()
}

/// Answers a scheduler loop with `answer(t, window)` until it converges.
fn drive(
    eng: &mut BoundaryEngine,
    mut answer: impl FnMut(f64, f64) -> (Option<Value>, Option<Vec<Value>>),
    max_probes: usize,
) -> usize {
    let mut n = 0;
    while let Some(plan) = eng.next_probe() {
        assert!(n < max_probes, "scheduler failed to converge");
        let (result, offsets) = answer(plan.t, plan.window);
        eng.add_probe(Probe {
            t: plan.t,
            window: plan.window,
            purpose: plan.purpose.as_str().into(),
            result,
            offsets,
        });
        n += 1;
    }
    n
}

fn titled(title: &str) -> Option<Value> {
    Some(json!({"artist": "X", "title": title, "confidence": 0.9}))
}

// ---- the engine --------------------------------------------------------------

#[test]
fn probe_mid() {
    assert_eq!(probe(100.0).0.mid(), 115.0);
}

#[test]
fn evidence_sorted_and_virtual_endpoints() {
    let mut eng = engine(600.0);
    add(&mut eng, probe(300.0).title("B"));
    add(&mut eng, probe(60.0).title("A"));
    let points = eng.points();
    assert_eq!(points[0].identity, Identity::Start);
    assert_eq!(points[points.len() - 1].identity, Identity::End);
    assert_eq!(mids(&points), [0.0, 75.0, 315.0, 600.0]);
}

#[test]
fn fuzzy_identity_collapses_metadata_drift() {
    let mut eng = engine(600.0);
    add(
        &mut eng,
        probe(60.0).title("One More Time").artist("Daft Punk"),
    );
    add(
        &mut eng,
        probe(150.0)
            .title("One More Time (Radio Edit)")
            .artist("Daft Punk"),
    );
    assert_eq!(eng.identities[0], eng.identities[1]);
}

#[test]
fn none_result_is_unidentified() {
    let mut eng = engine(600.0);
    add(&mut eng, probe(60.0));
    // An empty answer is no answer, as Python's `if not result` has it.
    eng.add_probe(Probe {
        result: Some(json!({})),
        ..probe(200.0).0
    });
    assert_eq!(
        eng.identities,
        [Identity::Unidentified, Identity::Unidentified]
    );
}

#[test]
fn targets_by_interval_kind() {
    let cfg = EngineConfig::default();
    let mut eng = engine(1000.0);
    add(&mut eng, probe(100.0).title("A"));
    add(&mut eng, probe(300.0).title("A"));
    add(&mut eng, probe(500.0).title("B"));
    add(&mut eng, probe(700.0)); // miss
    // START..A edge, A..A same track, A..B boundary, B..None, None..END edge.
    assert_eq!(
        targets(&eng),
        [
            cfg.stride,
            cfg.stride,
            cfg.precision,
            cfg.precision_none,
            cfg.stride
        ]
    );
}

#[test]
fn is_boundary_only_for_two_distinct_real_tracks() {
    let mut eng = engine(1000.0);
    add(&mut eng, probe(100.0).title("A"));
    add(&mut eng, probe(300.0).title("B"));
    add(&mut eng, probe(500.0));
    let flags: Vec<bool> = eng.pairs().map(|(l, r)| eng.is_boundary(l, r)).collect();
    assert_eq!(flags, [false, true, false, false]);
}

#[test]
fn cluster_meta_keeps_highest_confidence_variant() {
    let mut eng = engine(600.0);
    add(
        &mut eng,
        probe(60.0)
            .title("One More Time (Radio Edit)")
            .confidence(0.5),
    );
    add(
        &mut eng,
        probe(150.0).title("One More Time").confidence(0.9),
    );
    assert_eq!(
        eng.meta(eng.identities[0]).unwrap()["title"],
        "One More Time"
    );
}

#[test]
fn probe_start_estimate_corrects_for_the_centred_fingerprint_excerpt() {
    // A 30s probe's offset is measured 10s into its window and a 12s probe's 1s into its
    // own, so both imply the same start. Uncorrected they disagree by 9s.
    let eng = engine(1000.0);
    let coverage = probe(200.0).title("B").offset(60.0).0;
    let refine = probe(200.0).title("B").window(12.0).offset(51.0).0;
    assert_eq!(eng.probe_start_estimate(&coverage), Some(150.0));
    assert_eq!(eng.probe_start_estimate(&refine), Some(150.0));
}

#[test]
fn probe_start_estimate_ignores_timeskewed_matches() {
    let eng = engine(1000.0);
    let p = probe(200.0).title("B").skewed(50.0, 0.5).0;
    assert_eq!(eng.probe_start_estimate(&p), None);
}

#[test]
fn trusted_start_needs_corroboration_and_agreement() {
    let mut eng = engine(1000.0);
    add(&mut eng, probe(200.0).title("B").offset(60.0));
    let key = eng.identities[0];
    assert_eq!(eng.trusted_start(key), None); // min_corroboration = 2
    add(&mut eng, probe(290.0).title("B").offset(149.0));
    assert_eq!(eng.trusted_start(key), Some(150.5)); // median of 150.0, 151.0
    add(&mut eng, probe(380.0).title("B").offset(100.0)); // wildly off
    assert_eq!(eng.trusted_start(key), None); // spread > offset_tolerance
}

#[test]
fn resolved_by_prediction_requires_confirming_probe_after_p() {
    let mut eng = engine(1000.0);
    add(&mut eng, probe(60.0).title("A"));
    add(&mut eng, probe(200.0).title("B").offset(60.0));
    add(&mut eng, probe(290.0).title("B").offset(150.0));
    // P = 150, inside (75, 215), but no B probe starts within [149.5, 155].
    let (l, r) = boundary(&eng);
    assert_eq!(eng.resolved_by_prediction(l, r), None);
    add(
        &mut eng,
        probe(152.0).title("B").window(12.0).refine().offset(3.0),
    );
    let (l, r) = boundary(&eng);
    let resolved = eng.resolved_by_prediction(l, r).unwrap();
    assert!((resolved - 150.0).abs() < 1.0);
}

#[test]
fn first_probe_is_centre_of_file() {
    let plan = engine(14400.0).next_probe().unwrap();
    assert_eq!(plan.purpose, Purpose::Coverage);
    assert!((plan.t + plan.window / 2.0 - 7200.0).abs() < 45.0);
}

#[test]
fn tiny_file_still_gets_one_probe() {
    let mut eng = engine(60.0);
    let plan = eng.next_probe().unwrap();
    assert_eq!(plan.purpose, Purpose::Coverage);
    add(&mut eng, probe(plan.t).window(plan.window));
    assert_eq!(eng.next_probe(), None);
    // Under a second there's nothing to probe at all.
    assert_eq!(engine(1.0).next_probe(), None);
}

#[test]
fn coverage_lands_on_stride_grid_and_costs_duration_over_stride() {
    let mut eng = engine(1800.0); // 30 minutes, one track everywhere
    let n = drive(&mut eng, |_, _| (titled("A"), None), 500);
    // 20 intervals, so 19 interior grid points, give or take the edges.
    assert!(n <= 22, "{n}");
    let mut mids: Vec<f64> = eng.probes.iter().map(Probe::mid).collect();
    mids.sort_by(f64::total_cmp);
    let gaps: Vec<f64> = mids.windows(2).map(|w| w[1] - w[0]).collect();
    assert!(gaps.iter().all(|&g| g > 1.0 && g <= eng.cfg.stride + 0.5));
}

#[test]
fn boundary_refined_to_precision_without_offsets() {
    let mut eng = engine(600.0);
    let true_boundary = 293.0;
    drive(
        &mut eng,
        |t, w| {
            (
                titled(if t + w / 2.0 < true_boundary {
                    "A"
                } else {
                    "B"
                }),
                None,
            )
        },
        500,
    );
    let (l, r) = boundary(&eng);
    assert!(r.mid - l.mid <= eng.cfg.precision + 0.01);
}

#[test]
fn trusted_prediction_plans_verification_after_p() {
    let mut eng = engine(1000.0);
    add(&mut eng, probe(100.0).title("A")); // mid 115
    add(&mut eng, probe(160.0).title("B").offset(20.0)); // mid 175, implies 150
    add(&mut eng, probe(400.0).title("B").offset(260.0)); // corroborates 150
    // P = 150 inside (115, 175), which is under the stride, so it's the boundary's turn.
    let plan = eng.next_probe().unwrap();
    assert_eq!(plan.purpose, Purpose::Refine);
    // The fingerprinted excerpt, not the window, starts verify_lead after P.
    let excerpt_start = plan.t + eng.fingerprint_lead(plan.window);
    assert!((excerpt_start - (150.0 + eng.cfg.verify_lead)).abs() < 0.01);
    assert_eq!(plan.window, eng.cfg.refine_window);
}

#[test]
fn converges_even_when_oracle_is_noisy_at_boundary() {
    let mut rng = super::super::pyrandom::Random::new(7);
    let mut eng = engine(600.0);
    let true_boundary = 300.0;
    let n = drive(
        &mut eng,
        |t, w| {
            let mid = t + w / 2.0;
            let title = if (mid - true_boundary).abs() < w / 2.0 {
                *rng.choice(&["A", "B"]) // the window straddles: a coin flip
            } else if mid < true_boundary {
                "A"
            } else {
                "B"
            };
            (titled(title), None)
        },
        500,
    );
    assert!(n < 120, "{n}"); // bounded spend, no livelock
    assert_eq!(eng.next_probe(), None);
}

#[test]
fn add_probe_emits_probe_result_and_discovery() {
    let mut eng = engine(600.0);
    let evs = types(&add(&mut eng, probe(85.0).title("A")));
    assert!(evs.contains(&"probe_result".into()) && evs.contains(&"track_discovered".into()));
    let evs = types(&add(&mut eng, probe(185.0).title("A")));
    assert!(!evs.contains(&"track_discovered".into())); // a known cluster
}

#[test]
fn interval_split_and_retire_events() {
    let mut eng = engine(170.0);
    let evs = types(&add(&mut eng, probe(75.0).title("A"))); // mid 90
    assert!(evs.contains(&"interval_split".into()));
    // Both children (0..90, 90..170) are within the stride, so they're born retired.
    assert!(evs.contains(&"interval_retired".into()));
}

#[test]
fn boundary_predicted_fires_when_trust_established() {
    let mut eng = engine(1000.0);
    add(&mut eng, probe(60.0).title("A"));
    let evs = add(&mut eng, probe(300.0).title("B").offset(160.0));
    assert!(!types(&evs).contains(&"boundary_predicted".into())); // no corroboration yet
    let evs = add(&mut eng, probe(390.0).title("B").offset(250.0));
    let predicted: Vec<f64> = evs
        .iter()
        .filter_map(|e| match e {
            Event::BoundaryPredicted {
                predicted_start, ..
            } => Some(*predicted_start),
            _ => None,
        })
        .collect();
    assert!((predicted[0] - 150.0).abs() < 1.0);
}

#[test]
fn boundary_confirmed_fires_when_verification_lands() {
    let mut eng = engine(1000.0);
    add(&mut eng, probe(60.0).title("A"));
    add(&mut eng, probe(300.0).title("B").offset(160.0));
    add(&mut eng, probe(390.0).title("B").offset(250.0));
    let evs = add(
        &mut eng,
        probe(152.0).title("B").window(12.0).refine().offset(3.0),
    );
    let confirmed: Vec<f64> = evs
        .iter()
        .filter_map(|e| match e {
            Event::BoundaryConfirmed { start, .. } => Some(*start),
            _ => None,
        })
        .collect();
    assert!((confirmed[0] - 150.0).abs() < 1.0);
}

#[test]
fn cut_in_detected_when_verify_answers_previous_track() {
    let mut eng = engine(1000.0);
    add(&mut eng, probe(60.0).title("A"));
    // B cut in mid-song: its offsets imply a start at 150 but it began around 250.
    add(&mut eng, probe(300.0).title("B").offset(160.0));
    add(&mut eng, probe(390.0).title("B").offset(250.0));
    let evs = add(&mut eng, probe(152.0).title("A").window(12.0).refine());
    assert!(types(&evs).contains(&"cut_in_detected".into()));
}

#[test]
fn events_serialize_as_python_writes_them() {
    let mut eng = engine(600.0);
    let evs = add(&mut eng, probe(85.04).title("A"));
    assert_eq!(
        serde_json::to_string(&evs[0]).unwrap(),
        r#"{"type":"probe_result","t":85.0,"window":30.0,"purpose":"coverage","artist":"X","title":"A","confidence":0.9}"#
    );
    let drop = Dropped::PhantomDropped {
        kind: PhantomKind::Gap,
        name: None,
        start: 1.0,
        extent: 2.0,
    };
    assert_eq!(
        serde_json::to_string(&drop).unwrap(),
        r#"{"type":"phantom_dropped","kind":"gap","start":1.0,"extent":2.0}"#
    );
}

// ---- segments ----------------------------------------------------------------

#[test]
fn no_evidence_yields_one_unidentified_segment() {
    let (segs, drops) = engine(600.0).segments();
    assert_eq!(titles(&segs), [None]);
    assert_eq!(segs[0].start, 0.0);
    assert!(drops.is_empty());
    assert!(engine(0.0).segments().0.is_empty());
}

#[test]
fn two_tracks_boundary_at_gap_midpoint_when_unpredicted() {
    let mut eng = engine(600.0);
    add(&mut eng, probe(85.0).title("A")); // mid 100
    add(&mut eng, probe(285.0).title("B")); // mid 300
    let (segs, _) = eng.segments();
    assert_eq!(titles(&segs), [Some("A"), Some("B")]);
    assert_eq!(
        (segs[0].start, segs[0].confidence),
        (0.0, Confidence::Resolved)
    );
    assert_eq!(
        (segs[1].start, segs[1].confidence),
        (200.0, Confidence::Coarse)
    );
}

#[test]
fn resolved_prediction_places_boundary_at_p() {
    // All four imply a start at 150 once the centred excerpt's lead is accounted for.
    let mut eng = engine(600.0);
    add(&mut eng, probe(60.0).title("A"));
    add(&mut eng, probe(300.0).title("B").offset(160.0));
    add(&mut eng, probe(390.0).title("B").offset(250.0));
    add(
        &mut eng,
        probe(152.0).title("B").window(12.0).refine().offset(3.0),
    );
    let (segs, _) = eng.segments();
    let b = segs
        .iter()
        .find(|s| s.info.as_ref().is_some_and(|i| i["title"] == "B"))
        .unwrap();
    assert!((b.start - 150.0).abs() < 1.0);
    assert_eq!(b.confidence, Confidence::Resolved);
}

#[test]
fn phantom_single_probe_low_confidence_is_dropped_and_merged() {
    // Pinned on both sides: C's span is 238..251, 13s, under phantom_min.
    let mut eng = engine(600.0);
    add(&mut eng, probe(85.0).title("A"));
    add(&mut eng, probe(224.0).title("A").window(12.0).refine());
    add(
        &mut eng,
        probe(240.0)
            .title("C")
            .window(12.0)
            .refine()
            .confidence(0.2),
    );
    add(&mut eng, probe(250.0).title("A").window(12.0).refine());
    let (segs, drops) = eng.segments();
    assert_eq!(titles(&segs), [Some("A")]); // merged straight through
    assert_eq!(as_json(&drops)[0]["type"], "phantom_dropped");
}

#[test]
fn unpinned_single_probe_track_is_not_dropped() {
    // The same C with 146s of open water on its left: it may really span it.
    let mut eng = engine(600.0);
    add(&mut eng, probe(85.0).title("A"));
    add(
        &mut eng,
        probe(240.0)
            .title("C")
            .window(12.0)
            .refine()
            .confidence(0.2),
    );
    add(&mut eng, probe(250.0).title("A").window(12.0).refine());
    let (segs, drops) = eng.segments();
    assert!(named(&segs).contains(&"C"));
    assert!(drops.is_empty());
}

#[test]
fn confident_single_probe_track_survives() {
    let mut eng = engine(600.0);
    add(&mut eng, probe(85.0).title("A"));
    add(&mut eng, probe(224.0).title("A").window(12.0).refine());
    add(
        &mut eng,
        probe(240.0)
            .title("C")
            .window(12.0)
            .refine()
            .confidence(0.9),
    );
    add(&mut eng, probe(250.0).title("A").window(12.0).refine());
    let (segs, drops) = eng.segments();
    assert!(named(&segs).contains(&"C"));
    assert!(drops.is_empty());
}

#[test]
fn short_none_blip_is_absorbed() {
    let mut eng = engine(600.0);
    add(&mut eng, probe(85.0).title("A"));
    add(&mut eng, probe(224.0).title("A").window(12.0).refine());
    add(&mut eng, probe(240.0).window(12.0).refine()); // a None blip
    add(&mut eng, probe(250.0).title("A").window(12.0).refine());
    assert_eq!(titles(&eng.segments().0), [Some("A")]);
}

#[test]
fn long_none_region_becomes_unidentified_segment() {
    let mut eng = engine(900.0);
    add(&mut eng, probe(85.0).title("A"));
    add(&mut eng, probe(385.0));
    add(&mut eng, probe(485.0));
    add(&mut eng, probe(785.0).title("B"));
    assert_eq!(titles(&eng.segments().0), [Some("A"), None, Some("B")]);
}

#[test]
fn boundary_stats_counts_pairs_and_resolution() {
    let mut eng = engine(600.0);
    add(&mut eng, probe(85.0).title("A"));
    add(&mut eng, probe(285.0).title("B")); // a coarse boundary (gap 200)
    add(&mut eng, probe(430.0).title("C"));
    add(&mut eng, probe(433.0).title("C").window(12.0));
    assert_eq!(eng.boundary_stats(), (2, 0));
}

// ---- contradiction collapse: the real set ----------------------------------------
//
// The fixtures are verbatim slices of a real 4-hour set's probe file (see
// fixtures/README.md), so these two cases are the measurements, not illustrations.

fn fold_fixture(name: &str) -> (Vec<Segment>, Vec<Dropped>) {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("src/engine/fixtures/{name}.json"));
    let progress = load_progress(&path).unwrap();
    let mut eng = engine(progress.audio_duration.unwrap());
    for p in progress.probes {
        eng.add_probe(p);
    }
    eng.segments()
}

fn collapsed(drops: &[Dropped]) -> Vec<Value> {
    as_json(drops)
        .into_iter()
        .filter(|d| d["type"] == "contradiction_collapsed")
        .collect()
}

#[test]
fn real_alternating_transition_collapses_to_one_boundary() {
    // 3:44 of the real set: Boz Scaggs "Lowdown" gives way to Grant Green "Sookie Sookie
    // (Live)", and Shazam alternates Sookie and Us3 across the cut (Us3 sample Blue Note;
    // Grant Green is Blue Note). Unfixed, four segments spanning 36s.
    let (segs, drops) = fold_fixture("thrash_zone_probes");
    assert_eq!(
        titles(&segs),
        [Some("Lowdown"), Some("Sookie Sookie (Live)")]
    );
    assert!((13460.0..=13480.0).contains(&segs[1].start)); // ground truth ~3:44:2x
    assert_eq!(segs[1].confidence, Confidence::Coarse);
    let c = collapsed(&drops);
    assert_eq!(c.len(), 2);
    assert!(
        c.iter()
            .all(|d| d["kind"] == "thrash" && d["title"] == "Tukka Yoot's Riddim")
    );
    assert!(
        c.iter()
            .all(|d| d["winner_title"] == "Sookie Sookie (Live)")
    );
}

#[test]
fn real_sampled_track_sliver_is_absorbed_into_its_transition() {
    // 1:06: Roy Ayers gives way to Herb Alpert's "Rise", and the 16s between reads as
    // "Hypnotize", which samples Rise. Three distinct identities, both probes at 0.999:
    // only the extent gives it away.
    let (segs, drops) = fold_fixture("sliver_zone_probes");
    assert_eq!(titles(&segs), [Some("Pricilla's Theme"), Some("Rise")]);
    assert!((3973.0..=3990.0).contains(&segs[1].start));
    assert_eq!(segs[1].confidence, Confidence::Coarse);
    let c = collapsed(&drops);
    assert_eq!(c.len(), 1);
    assert_eq!(
        (c[0]["kind"].as_str(), c[0]["title"].as_str()),
        (Some("sliver"), Some("Hypnotize"))
    );
}

// ---- contradiction collapse: synthetic -------------------------------------------

/// A refine probe centred on `mid` whose offset implies the track began at `start`,
/// measured from the centred excerpt as Shazam does.
fn refine(mid: f64, title: Option<&str>, start: Option<f64>, confidence: f64) -> Probe {
    let window = 12.0;
    let t = mid - window / 2.0;
    let mut p = probe(t).window(window).refine();
    if let Some(title) = title {
        p = p.title(title).confidence(confidence);
    }
    if let Some(start) = start {
        let lead = f64::max(0.0, (window - 10.0) / 2.0);
        p = p.offset(crate::engine::py::round_to(t + lead - start, 3));
    }
    p.0
}

fn r(mid: f64, title: &str, start: f64) -> Probe {
    refine(mid, Some(title), Some(start), 0.9)
}

#[test]
fn alternating_identities_collapse_to_the_better_supported_one() {
    // Every run is longer than phantom_min, so only the A-B-A-B pattern opens the question,
    // and only X's offsets (Y's start throughout) answer it.
    let mut eng = engine(1200.0);
    eng.add_probe(r(100.0, "A", 0.0));
    eng.add_probe(r(196.0, "A", 0.0));
    eng.add_probe(r(200.0, "X", 190.0));
    eng.add_probe(r(220.0, "X", 190.0));
    for mid in [224.0, 234.0, 244.0] {
        eng.add_probe(r(mid, "Y", 190.0));
    }
    eng.add_probe(r(248.0, "X", 190.0));
    eng.add_probe(r(268.0, "X", 190.0));
    for mid in [272.0, 340.0, 400.0, 460.0] {
        eng.add_probe(r(mid, "Y", 190.0));
    }
    let (segs, drops) = eng.segments();
    assert_eq!(titles(&segs), [Some("A"), Some("Y")]); // X loses 4 probes to 7
    assert_eq!(segs[1].confidence, Confidence::Coarse);
    let c = collapsed(&drops);
    assert_eq!(c.len(), 2);
    for d in &c {
        assert_eq!(
            (&d["kind"], &d["title"], &d["winner_title"]),
            (&json!("thrash"), &json!("X"), &json!("Y"))
        );
        assert!(d["extent"].as_f64().unwrap() >= EngineConfig::default().phantom_min);
    }
}

#[test]
fn one_excursion_is_not_a_transition_zone() {
    // A B A is a single return, which the confidence rule still owns.
    let mut eng = engine(1200.0);
    eng.add_probe(r(100.0, "A", 0.0));
    eng.add_probe(r(196.0, "A", 0.0));
    eng.add_probe(r(200.0, "B", 190.0));
    eng.add_probe(r(220.0, "B", 190.0));
    for mid in [224.0, 300.0, 400.0] {
        eng.add_probe(r(mid, "A", 190.0));
    }
    let (segs, drops) = eng.segments();
    assert!(named(&segs).contains(&"B"));
    assert!(drops.is_empty());
}

#[test]
fn thrash_collapse_spares_a_long_run_of_the_losing_identity() {
    // The zone reaches back into A's real 104s run; that run is longer than the stride and
    // its offsets are its own, so only A's second, 24s run (B's audio) goes.
    let mut eng = engine(1200.0);
    eng.add_probe(r(100.0, "Z", 0.0));
    eng.add_probe(r(196.0, "Z", 0.0));
    eng.add_probe(r(200.0, "A", 190.0));
    eng.add_probe(r(300.0, "A", 190.0));
    for mid in [304.0, 314.0, 324.0] {
        eng.add_probe(r(mid, "B", 295.0));
    }
    eng.add_probe(r(328.0, "A", 295.0)); // A, but hearing B
    eng.add_probe(r(348.0, "A", 295.0));
    for mid in [352.0, 420.0, 480.0, 540.0] {
        eng.add_probe(r(mid, "B", 295.0));
    }
    let (segs, drops) = eng.segments();
    assert_eq!(titles(&segs), [Some("Z"), Some("A"), Some("B")]);
    assert_eq!(segs[1].start, 198.0);
    let c = collapsed(&drops);
    assert_eq!(c.len(), 1);
    assert_eq!(c[0]["title"], "A");
    assert!(c[0]["extent"].as_f64().unwrap() < EngineConfig::default().stride);
}

#[test]
fn multi_probe_sliver_between_resolved_boundaries_is_collapsed() {
    // Neither probe count nor confidence saves a run whose 8s implies C's start.
    let mut eng = engine(1200.0);
    eng.add_probe(r(100.0, "A", 0.0));
    eng.add_probe(r(200.0, "A", 0.0));
    eng.add_probe(r(204.0, "A", 0.0));
    eng.add_probe(refine(208.0, Some("B"), Some(200.0), 0.99));
    eng.add_probe(refine(212.0, Some("B"), Some(200.0), 0.99));
    for mid in [216.0, 220.0, 300.0] {
        eng.add_probe(r(mid, "C", 200.0));
    }
    let (segs, drops) = eng.segments();
    assert_eq!(titles(&segs), [Some("A"), Some("C")]);
    assert_eq!(segs[1].confidence, Confidence::Coarse);
    let d = as_json(&drops);
    assert_eq!(d.len(), 1);
    assert_eq!(
        (&d[0]["kind"], &d[0]["reason"]),
        (&json!("sliver"), &json!("collide"))
    );
}

#[test]
fn the_same_sliver_survives_while_its_boundaries_are_coarse() {
    // Wider gaps: the 14s extent is a guess, not a measurement, so the rule waits.
    let mut eng = engine(1200.0);
    eng.add_probe(r(100.0, "A", 0.0));
    eng.add_probe(r(200.0, "A", 0.0));
    eng.add_probe(refine(210.0, Some("B"), Some(195.0), 0.99));
    eng.add_probe(refine(214.0, Some("B"), Some(195.0), 0.99));
    eng.add_probe(r(224.0, "C", 195.0));
    eng.add_probe(r(300.0, "C", 195.0));
    let (segs, drops) = eng.segments();
    assert_eq!(titles(&segs), [Some("A"), Some("B"), Some("C")]);
    assert!(drops.is_empty());
}

#[test]
fn a_sliver_flanked_by_unidentified_audio_survives() {
    // Unidentified is no answer, not a contradicting one.
    let mut eng = engine(1200.0);
    eng.add_probe(refine(200.0, None, None, 0.9));
    eng.add_probe(refine(206.0, Some("X"), Some(195.0), 0.9));
    eng.add_probe(refine(212.0, None, None, 0.9));
    eng.add_probe(refine(300.0, None, None, 0.9));
    let (segs, drops) = eng.segments();
    assert!(named(&segs).contains(&"X"));
    assert!(drops.is_empty());
}

#[test]
fn a_genuine_short_track_between_two_long_ones_survives() {
    // 44s is a track, and its offsets imply its own start.
    let mut eng = engine(1200.0);
    eng.add_probe(r(100.0, "A", 0.0));
    eng.add_probe(r(200.0, "A", 0.0));
    eng.add_probe(r(204.0, "S", 198.0));
    eng.add_probe(r(244.0, "S", 198.0));
    eng.add_probe(r(248.0, "C", 242.0));
    eng.add_probe(r(300.0, "C", 242.0));
    let (segs, drops) = eng.segments();
    assert_eq!(titles(&segs), [Some("A"), Some("S"), Some("C")]);
    assert!(drops.is_empty());
}

#[test]
fn unidentified_dropout_inside_one_track_is_absorbed() {
    // A 22.5s hole between two runs of one track is a dropout, judged at precision_none:
    // the real set showed The J.B.'s as three rows around one.
    let mut eng = engine(1200.0);
    add(&mut eng, probe(185.0).title("A")); // mid 200
    add(&mut eng, probe(207.5)); // None, mid 222.5
    add(&mut eng, probe(230.0).title("A")); // mid 245
    add(&mut eng, probe(320.0).title("A")); // mid 335
    let (segs, drops) = eng.segments();
    assert_eq!(titles(&segs), [Some("A")]);
    assert_eq!(
        as_json(&drops),
        [json!({"type": "phantom_dropped", "kind": "dropout", "start": 211.2, "extent": 22.5})]
    );
}

#[test]
fn an_unidentified_stretch_between_two_tracks_is_kept() {
    let mut eng = engine(1200.0);
    add(&mut eng, probe(185.0).title("A"));
    add(&mut eng, probe(207.5));
    add(&mut eng, probe(230.0).title("B"));
    add(&mut eng, probe(320.0).title("B"));
    let (segs, drops) = eng.segments();
    assert_eq!(titles(&segs), [Some("A"), None, Some("B")]);
    assert!(drops.is_empty());
}
