"""BoundaryEngine core: identity clustering, evidence ordering, interval targets."""

from setlist_maker.boundary import (
    END,
    START,
    BoundaryEngine,
    EngineConfig,
    Probe,
)


def probe(
    t, artist=None, title=None, window=30.0, purpose="coverage", confidence=0.9, offsets=None
):
    result = None
    if title is not None:
        result = {"artist": artist or "X", "title": title, "confidence": confidence}
    return Probe(t=t, window=window, purpose=purpose, result=result, offsets=offsets)


def test_probe_mid():
    assert probe(100.0).mid == 115.0


def test_evidence_sorted_and_virtual_endpoints():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(300.0, title="B"))
    eng.add_probe(probe(60.0, title="A"))
    points = eng._points()
    assert points[0].identity is START and points[0].mid == 0.0
    assert points[-1].identity is END and points[-1].mid == 600.0
    assert [p.mid for p in points[1:-1]] == [75.0, 315.0]


def test_fuzzy_identity_collapses_metadata_drift():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(60.0, artist="Daft Punk", title="One More Time"))
    eng.add_probe(probe(150.0, artist="Daft Punk", title="One More Time (Radio Edit)"))
    ids = eng._identity_by_index
    assert ids[0] == ids[1]


def test_none_result_is_none_identity():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(60.0))
    assert eng._identity_by_index == [None]


def test_targets_by_interval_kind():
    cfg = EngineConfig()
    eng = BoundaryEngine(1000.0, cfg)
    eng.add_probe(probe(100.0, title="A"))
    eng.add_probe(probe(300.0, title="A"))
    eng.add_probe(probe(500.0, title="B"))
    eng.add_probe(probe(700.0))  # miss
    pts = eng._points()
    # START..A edge, A..A same-track -> stride; A..B boundary -> precision;
    # B..None -> precision_none; None..END edge -> stride.
    targets = [eng._target(left, right) for left, right in zip(pts, pts[1:])]
    assert targets == [cfg.stride, cfg.stride, cfg.precision, cfg.precision_none, cfg.stride]


def test_is_boundary_only_for_two_distinct_real_tracks():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(100.0, title="A"))
    eng.add_probe(probe(300.0, title="B"))
    eng.add_probe(probe(500.0))
    pts = eng._points()
    flags = [eng._is_boundary(left, right) for left, right in zip(pts, pts[1:])]
    assert flags == [False, True, False, False]


def test_cluster_meta_keeps_highest_confidence_variant():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(60.0, title="One More Time (Radio Edit)", confidence=0.5))
    eng.add_probe(probe(150.0, title="One More Time", confidence=0.9))
    key = eng._identity_by_index[0]
    assert eng._cluster_meta[key]["title"] == "One More Time"


def off(o, skew=0.0):
    return {"offset": o, "timeskew": skew}


def test_probe_start_estimate_corrects_for_the_centered_fingerprint_excerpt():
    """Not plain T - O: shazamio_core fingerprints a centered 10s excerpt, so a
    30s probe's offset describes audio 10s into its window and a 12s probe's
    1s into its own. Both must therefore imply the *same* start -- measured on
    a real recording, they agree to 0.1s (spec Errata). Left uncorrected the
    two disagree by 9s, which exceeds offset_tolerance and silently disables
    prediction for every track probed at both window sizes."""
    eng = BoundaryEngine(1000.0)
    coverage = probe(200.0, title="B", window=30.0, offsets=[off(60.0)])
    refine = probe(200.0, title="B", window=12.0, offsets=[off(51.0)])
    assert eng._probe_start_estimate(coverage) == 150.0
    assert eng._probe_start_estimate(refine) == 150.0


def test_probe_start_estimate_ignores_timeskewed_matches():
    eng = BoundaryEngine(1000.0)
    p = probe(200.0, title="B", offsets=[off(50.0, skew=0.5)])
    assert eng._probe_start_estimate(p) is None


def test_trusted_start_needs_corroboration_and_agreement():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(200.0, title="B", offsets=[off(60.0)]))
    key = eng._identity_by_index[0]
    assert eng._trusted_start(key) is None  # min_corroboration = 2
    eng.add_probe(probe(290.0, title="B", offsets=[off(149.0)]))
    assert eng._trusted_start(key) == 150.5  # median of 150.0, 151.0
    eng.add_probe(probe(380.0, title="B", offsets=[off(100.0)]))  # wildly off
    assert eng._trusted_start(key) is None  # spread > offset_tolerance


def test_resolved_by_prediction_requires_confirming_probe_after_p():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    eng.add_probe(probe(200.0, title="B", offsets=[off(60.0)]))
    eng.add_probe(probe(290.0, title="B", offsets=[off(150.0)]))
    pts = eng._points()
    boundary = next(
        (left, right) for left, right in zip(pts, pts[1:]) if eng._is_boundary(left, right)
    )
    # P = 150, inside (75, 215); but no B probe *starts* within [149.5, 155].
    assert eng._resolved_by_prediction(*boundary) is None
    eng.add_probe(probe(152.0, title="B", window=12.0, purpose="refine", offsets=[off(3.0)]))
    pts = eng._points()
    boundary = next(
        (left, right) for left, right in zip(pts, pts[1:]) if eng._is_boundary(left, right)
    )
    resolved = eng._resolved_by_prediction(*boundary)
    assert resolved is not None and abs(resolved - 150.0) < 1.0


def drive(eng, answer, max_probes=500):
    """Loop next_probe -> add_probe, answering via `answer(t, window)`."""
    n = 0
    while (plan := eng.next_probe()) is not None:
        assert n < max_probes, "scheduler failed to converge"
        result, offsets = answer(plan.t, plan.window)
        eng.add_probe(
            Probe(
                t=plan.t, window=plan.window, purpose=plan.purpose, result=result, offsets=offsets
            )
        )
        n += 1
    return n


def test_first_probe_is_center_of_file():
    eng = BoundaryEngine(14400.0)
    plan = eng.next_probe()
    assert plan.purpose == "coverage"
    assert abs((plan.t + plan.window / 2) - 7200.0) < 45.0  # near center, on grid


def test_tiny_file_still_gets_one_probe():
    eng = BoundaryEngine(60.0)
    plan = eng.next_probe()
    assert plan is not None and plan.purpose == "coverage"
    eng.add_probe(Probe(plan.t, plan.window, plan.purpose, None, None))
    assert eng.next_probe() is None


def test_coverage_lands_on_stride_grid_and_costs_duration_over_stride():
    eng = BoundaryEngine(1800.0)  # 30 min, one track everywhere

    def answer(t, w):
        return ({"artist": "X", "title": "A", "confidence": 0.9}, None)

    n = drive(eng, answer)
    # duration/stride = 20 intervals -> 19 interior grid points, +-2 for edges
    assert n <= 22
    mids = sorted(p.mid for p in eng.probes)
    gaps = [b - a for a, b in zip(mids, mids[1:])]
    assert max(gaps) <= eng.cfg.stride + 0.5
    assert all(g > 1.0 for g in gaps)


def test_boundary_refined_to_precision_without_offsets():
    eng = BoundaryEngine(600.0)
    true_boundary = 293.0

    def answer(t, w):
        mid = t + w / 2
        title = "A" if mid < true_boundary else "B"
        return ({"artist": "X", "title": title, "confidence": 0.9}, None)

    drive(eng, answer)
    pts = eng._points()
    boundary = next(
        (left, right) for left, right in zip(pts, pts[1:]) if eng._is_boundary(left, right)
    )
    assert boundary[1].mid - boundary[0].mid <= eng.cfg.precision + 0.01


def test_trusted_prediction_plans_verification_after_p():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    eng.add_probe(probe(300.0, title="B", offsets=[off(160.0)]))
    eng.add_probe(probe(390.0, title="B", offsets=[off(250.0)]))
    # P = 150 inside (75, 315): expect verify at P + verify_lead with refine window.
    plan = eng.next_probe()
    assert plan.purpose == "refine"
    assert abs(plan.t - (150.0 + eng.cfg.verify_lead)) < 0.01
    assert plan.window == eng.cfg.refine_window


def test_converges_even_when_oracle_is_noisy_at_boundary():
    import random

    rng = random.Random(7)
    eng = BoundaryEngine(600.0)
    true_boundary = 300.0

    def answer(t, w):
        mid = t + w / 2
        if abs(mid - true_boundary) < w / 2:  # window straddles: coin flip
            title = rng.choice(["A", "B"])
        else:
            title = "A" if mid < true_boundary else "B"
        return ({"artist": "X", "title": title, "confidence": 0.9}, None)

    n = drive(eng, answer)
    assert n < 120  # bounded spend, no livelock
    assert eng.next_probe() is None


def _types(events):
    return [e["type"] for e in events]


def test_add_probe_emits_probe_result_and_discovery():
    eng = BoundaryEngine(600.0)
    evs = eng.add_probe(probe(85.0, title="A"))
    assert "probe_result" in _types(evs) and "track_discovered" in _types(evs)
    evs = eng.add_probe(probe(185.0, title="A"))
    assert "track_discovered" not in _types(evs)  # known cluster


def test_interval_split_and_retire_events():
    eng = BoundaryEngine(170.0)
    evs = eng.add_probe(probe(75.0, title="A"))  # mid 90
    assert "interval_split" in _types(evs)
    # Both children (0..90, 90..170) are within the 90s stride -> born retired.
    assert "interval_retired" in _types(evs)


def test_boundary_predicted_fires_when_trust_established():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    evs = eng.add_probe(probe(300.0, title="B", offsets=[off(160.0)]))
    assert "boundary_predicted" not in _types(evs)  # one probe, no corroboration
    evs = eng.add_probe(probe(390.0, title="B", offsets=[off(250.0)]))
    predicted = [e for e in evs if e["type"] == "boundary_predicted"]
    assert predicted and abs(predicted[0]["predicted_start"] - 150.0) < 1.0


def test_boundary_confirmed_fires_when_verification_lands():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    eng.add_probe(probe(300.0, title="B", offsets=[off(160.0)]))
    eng.add_probe(probe(390.0, title="B", offsets=[off(250.0)]))
    evs = eng.add_probe(probe(152.0, title="B", window=12.0, purpose="refine", offsets=[off(3.0)]))
    confirmed = [e for e in evs if e["type"] == "boundary_confirmed"]
    assert confirmed and abs(confirmed[0]["start"] - 150.0) < 1.0


def test_cut_in_detected_when_verify_answers_previous_track():
    eng = BoundaryEngine(1000.0)
    eng.add_probe(probe(60.0, title="A"))
    # B cut in mid-song: offsets imply start 150 but B really began ~250.
    eng.add_probe(probe(300.0, title="B", offsets=[off(160.0)]))
    eng.add_probe(probe(390.0, title="B", offsets=[off(250.0)]))
    evs = eng.add_probe(probe(152.0, title="A", window=12.0, purpose="refine"))
    assert "cut_in_detected" in _types(evs)
