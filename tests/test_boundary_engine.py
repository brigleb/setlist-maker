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
