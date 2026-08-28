"""segments(): folding evidence into a tracklist, with phantom handling."""

from setlist_maker.boundary import BoundaryEngine, EngineConfig, Probe


def probe(
    t, artist=None, title=None, window=30.0, purpose="coverage", confidence=0.9, offsets=None
):
    result = None
    if title is not None:
        result = {"artist": artist or "X", "title": title, "confidence": confidence}
    return Probe(t=t, window=window, purpose=purpose, result=result, offsets=offsets)


def test_no_evidence_yields_one_unidentified_segment():
    segs, drops = BoundaryEngine(600.0).segments()
    assert len(segs) == 1 and segs[0].info is None and segs[0].start == 0.0
    assert drops == []


def test_two_tracks_boundary_at_gap_midpoint_when_unpredicted():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))  # mid 100
    eng.add_probe(probe(285.0, title="B"))  # mid 300
    segs, _ = eng.segments()
    assert [s.info["title"] if s.info else None for s in segs] == ["A", "B"]
    assert segs[0].start == 0.0 and segs[0].confidence == "resolved"
    assert segs[1].start == 200.0 and segs[1].confidence == "coarse"  # gap 200 > 5


def test_resolved_prediction_places_boundary_at_p():
    # Offsets carry the shazamio_core centered-excerpt lead: a 30s probe's
    # offset is measured 10s into its window, a 12s probe's 1s into its own.
    # All four below imply the same start, 150.0 (see spec Errata).
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(60.0, title="A"))
    eng.add_probe(probe(300.0, title="B", offsets=[{"offset": 160.0, "timeskew": 0.0}]))
    eng.add_probe(probe(390.0, title="B", offsets=[{"offset": 250.0, "timeskew": 0.0}]))
    eng.add_probe(
        probe(
            152.0,
            title="B",
            window=12.0,
            purpose="refine",
            offsets=[{"offset": 3.0, "timeskew": 0.0}],
        )
    )
    segs, _ = eng.segments()
    b = next(s for s in segs if s.info and s.info["title"] == "B")
    assert abs(b.start - 150.0) < 1.0 and b.confidence == "resolved"


def test_phantom_single_probe_low_confidence_is_dropped_and_merged():
    # A phantom's span is measured boundary-to-boundary, so it must be PINNED
    # by contradicting evidence on BOTH sides before it reads as small -- a
    # lone C with a wide-open flank might genuinely span that flank, and the
    # rule deliberately refuses to drop it until refinement squeezes it.
    cfg = EngineConfig(phantom_min=20.0, singleton_confidence_keep=0.6)
    eng = BoundaryEngine(600.0, cfg)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(224.0, title="A", window=12.0, purpose="refine"))
    eng.add_probe(probe(240.0, title="C", window=12.0, purpose="refine", confidence=0.2))
    eng.add_probe(probe(250.0, title="A", window=12.0, purpose="refine"))
    # C's span: (230+246)/2 .. (246+256)/2 = 238..251 -> 13s < phantom_min.
    segs, drops = eng.segments()
    assert [s.info["title"] for s in segs if s.info] == ["A"]
    assert len(segs) == 1  # merged straight through the dropped phantom
    assert any(d["type"] == "phantom_dropped" for d in drops)


def test_unpinned_single_probe_track_is_not_dropped():
    # Same C, but its left flank is 146s of open water: it may really span it.
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(240.0, title="C", window=12.0, purpose="refine", confidence=0.2))
    eng.add_probe(probe(250.0, title="A", window=12.0, purpose="refine"))
    segs, drops = eng.segments()
    assert "C" in [s.info["title"] for s in segs if s.info]
    assert drops == []


def test_confident_single_probe_track_survives():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(224.0, title="A", window=12.0, purpose="refine"))
    eng.add_probe(probe(240.0, title="C", window=12.0, purpose="refine", confidence=0.9))
    eng.add_probe(probe(250.0, title="A", window=12.0, purpose="refine"))
    segs, drops = eng.segments()
    assert "C" in [s.info["title"] for s in segs if s.info]
    assert drops == []


def test_short_none_blip_is_absorbed():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(224.0, title="A", window=12.0, purpose="refine"))
    eng.add_probe(probe(240.0, window=12.0, purpose="refine"))  # None blip
    eng.add_probe(probe(250.0, title="A", window=12.0, purpose="refine"))
    segs, drops = eng.segments()
    assert len(segs) == 1 and segs[0].info["title"] == "A"


def test_long_none_region_becomes_unidentified_segment():
    eng = BoundaryEngine(900.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(385.0))
    eng.add_probe(probe(485.0))
    eng.add_probe(probe(785.0, title="B"))
    segs, _ = eng.segments()
    assert [s.info["title"] if s.info else None for s in segs] == ["A", None, "B"]


def test_boundary_stats_counts_pairs_and_resolution():
    eng = BoundaryEngine(600.0)
    eng.add_probe(probe(85.0, title="A"))
    eng.add_probe(probe(285.0, title="B"))  # coarse boundary (gap 200)
    eng.add_probe(probe(430.0, title="C"))
    eng.add_probe(probe(433.0, title="C", window=12.0))
    found, at_target = eng.boundary_stats()
    assert found == 2 and at_target == 0
