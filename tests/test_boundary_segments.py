"""segments(): folding evidence into a tracklist, with phantom handling."""

from pathlib import Path

from setlist_maker.adaptive import load_probes
from setlist_maker.boundary import BoundaryEngine, EngineConfig, Probe

FIXTURES = Path(__file__).parent / "fixtures"


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


# --- contradiction collapse -------------------------------------------------
#
# Two readings of one idea: the evidence claims something shorter than a track.
# The fixtures are verbatim slices of a real 4-hour set's probe file (see
# tests/fixtures/README.md), so these two cases are the measurements, not
# illustrations.


def _fold_fixture(name):
    probes, duration = load_probes(FIXTURES / f"{name}.json")
    eng = BoundaryEngine(duration)
    for p in probes:
        eng.add_probe(p)
    return eng.segments()


def _titles(segs):
    return [s.info["title"] if s.info else None for s in segs]


def test_real_alternating_transition_collapses_to_one_boundary():
    # 3:44 of the real set: Boz Scaggs "Lowdown" gives way to Grant Green
    # "Sookie Sookie (Live)" and Shazam alternates Sookie/Us3 across the cut
    # (Us3 sample Blue Note records; Grant Green *is* Blue Note). Four segments
    # spanning 36s, every boundary between them "resolved" -- the engine probed
    # the zone to convergence and faithfully reported the thrash.
    segs, drops = _fold_fixture("thrash_zone_probes")
    assert _titles(segs) == ["Lowdown", "Sookie Sookie (Live)"]
    assert 13460.0 <= segs[1].start <= 13480.0  # ground truth ~3:44:2x
    assert segs[1].confidence == "coarse"
    collapsed = [d for d in drops if d["type"] == "contradiction_collapsed"]
    assert [d["kind"] for d in collapsed] == ["thrash", "thrash"]
    assert {d["title"] for d in collapsed} == {"Tukka Yoot's Riddim"}
    assert all(d["winner_title"] == "Sookie Sookie (Live)" for d in collapsed)


def test_real_sampled_track_sliver_is_absorbed_into_its_transition():
    # 1:06 of the same set: Roy Ayers gives way to Herb Alpert's "Rise", and
    # the 16s in between reads as Notorious B.I.G.'s "Hypnotize" -- which
    # samples Rise. Not an alternation: three distinct identities in a row,
    # both boundaries resolved, both probes at 0.999 confidence. Only the
    # *extent* gives it away.
    segs, drops = _fold_fixture("sliver_zone_probes")
    assert _titles(segs) == ["Pricilla's Theme", "Rise"]
    assert 3973.0 <= segs[1].start <= 3990.0
    assert segs[1].confidence == "coarse"
    collapsed = [d for d in drops if d["type"] == "contradiction_collapsed"]
    assert len(collapsed) == 1
    assert collapsed[0]["kind"] == "sliver"
    assert collapsed[0]["title"] == "Hypnotize"


def refine(mid, title=None, *, start=None, artist=None, confidence=0.9, window=12.0):
    """A refine probe centred on `mid`, whose offset implies the track began at
    `start`. Shazam measures the offset from the *centered excerpt*, not the
    window, so the arithmetic mirrors `_probe_start_estimate` (spec Errata 2) --
    hand-written offsets that skip the lead would be off by 1s here and 10s for
    a coverage probe, and the veto compares them to within 4s."""
    t = mid - window / 2.0
    offsets = None
    if start is not None:
        lead = max(0.0, (window - 10.0) / 2.0)
        offsets = [{"offset": round(t + lead - start, 3), "timeskew": 0.0}]
    return probe(
        t,
        artist=artist,
        title=title,
        window=window,
        purpose="refine",
        confidence=confidence,
        offsets=offsets,
    )


def test_alternating_identities_collapse_to_the_better_supported_one():
    # The thrash branch earning its own keep: every run here is longer than
    # phantom_min, so only the A-B-A-B *pattern* opens the question -- and only
    # X's offsets answer it. X implies the same start as Y throughout, which is
    # what a track being heard inside another one's audio looks like.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(refine(100.0, title="A", start=0.0))
    eng.add_probe(refine(196.0, title="A", start=0.0))
    eng.add_probe(refine(200.0, title="X", start=190.0))
    eng.add_probe(refine(220.0, title="X", start=190.0))
    for mid in (224.0, 234.0, 244.0):
        eng.add_probe(refine(mid, title="Y", start=190.0))
    eng.add_probe(refine(248.0, title="X", start=190.0))
    eng.add_probe(refine(268.0, title="X", start=190.0))
    for mid in (272.0, 340.0, 400.0, 460.0):
        eng.add_probe(refine(mid, title="Y", start=190.0))
    segs, drops = eng.segments()
    assert _titles(segs) == ["A", "Y"]  # X loses 4 probes to 7
    assert segs[1].confidence == "coarse"
    collapsed = [d for d in drops if d["type"] == "contradiction_collapsed"]
    assert [(d["kind"], d["title"], d["winner_title"]) for d in collapsed] == [
        ("thrash", "X", "Y"),
        ("thrash", "X", "Y"),
    ]
    assert all(d["extent"] >= EngineConfig().phantom_min for d in collapsed)


def test_one_excursion_is_not_a_transition_zone():
    # A B A is a single return, not an alternation -- the shape the sequential
    # path smoothed and the confidence rule still owns. Two returns, or none.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(refine(100.0, title="A", start=0.0))
    eng.add_probe(refine(196.0, title="A", start=0.0))
    eng.add_probe(refine(200.0, title="B", start=190.0))
    eng.add_probe(refine(220.0, title="B", start=190.0))
    for mid in (224.0, 300.0, 400.0):
        eng.add_probe(refine(mid, title="A", start=190.0))
    segs, drops = eng.segments()
    assert "B" in _titles(segs)
    assert drops == []


def test_thrash_collapse_spares_a_long_run_of_the_losing_identity():
    # The zone reaches back into the *preceding* track, because that track is
    # one end of its own return (A ... B ... A ... B). A's first run is real and
    # 104s long; only its second, 24s run is B's audio. Two things keep the real
    # one: it is longer than the coverage stride -- a span the engine's own
    # guarantee says cannot hide a track, so deleting it would discard evidence
    # that guarantee rests on -- and its offsets are its own, not B's.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(refine(100.0, title="Z", start=0.0))
    eng.add_probe(refine(196.0, title="Z", start=0.0))
    eng.add_probe(refine(200.0, title="A", start=190.0))
    eng.add_probe(refine(300.0, title="A", start=190.0))
    for mid in (304.0, 314.0, 324.0):
        eng.add_probe(refine(mid, title="B", start=295.0))
    eng.add_probe(refine(328.0, title="A", start=295.0))  # A, but hearing B
    eng.add_probe(refine(348.0, title="A", start=295.0))
    for mid in (352.0, 420.0, 480.0, 540.0):
        eng.add_probe(refine(mid, title="B", start=295.0))
    segs, drops = eng.segments()
    assert _titles(segs) == ["Z", "A", "B"]
    assert segs[1].start == 198.0  # the real A run, 104s of it, untouched
    collapsed = [d for d in drops if d["type"] == "contradiction_collapsed"]
    assert len(collapsed) == 1
    assert collapsed[0]["title"] == "A" and collapsed[0]["extent"] < EngineConfig().stride


def test_multi_probe_sliver_between_resolved_boundaries_is_collapsed():
    # The synthetic form of the Hypnotize case: neither probe count nor
    # confidence saves a run whose 8s of audio implies C's start, not its own.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(refine(100.0, title="A", start=0.0))
    eng.add_probe(refine(200.0, title="A", start=0.0))
    eng.add_probe(refine(204.0, title="A", start=0.0))
    eng.add_probe(refine(208.0, title="B", start=200.0, confidence=0.99))
    eng.add_probe(refine(212.0, title="B", start=200.0, confidence=0.99))
    for mid in (216.0, 220.0, 300.0):
        eng.add_probe(refine(mid, title="C", start=200.0))
    segs, drops = eng.segments()
    assert _titles(segs) == ["A", "C"]
    assert segs[1].confidence == "coarse"
    assert [(d["kind"], d["reason"]) for d in drops] == [("sliver", "collide")]


def test_the_same_sliver_survives_while_its_boundaries_are_coarse():
    # Identical evidence, wider probe gaps: the 14s extent is a not-yet-narrowed
    # guess rather than a measurement, so the rule refuses to act on it. This is
    # what stops the collapse racing the scheduler on an anytime read -- and it
    # is the only difference from the test above, which collapses.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(refine(100.0, title="A", start=0.0))
    eng.add_probe(refine(200.0, title="A", start=0.0))
    eng.add_probe(refine(210.0, title="B", start=195.0, confidence=0.99))
    eng.add_probe(refine(214.0, title="B", start=195.0, confidence=0.99))
    eng.add_probe(refine(224.0, title="C", start=195.0))
    eng.add_probe(refine(300.0, title="C", start=195.0))
    segs, drops = eng.segments()
    assert _titles(segs) == ["A", "B", "C"]
    assert drops == []


def test_a_sliver_flanked_by_unidentified_audio_survives():
    # `None` is the absence of an answer, not a contradicting claim, so a short
    # run inside an unidentified stretch is left to the existing confidence
    # rule. Measured consequence on the real set: this is what spares "Our
    # Voyage" (12.7s between two gaps) and "Deomid" (12.7s between a gap and a
    # track) while still collapsing the three real phantoms.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(refine(200.0))  # None
    eng.add_probe(refine(206.0, title="X", start=195.0, confidence=0.9))
    eng.add_probe(refine(212.0))  # None
    eng.add_probe(refine(300.0))  # None
    segs, drops = eng.segments()
    assert "X" in _titles(segs)
    assert drops == []


def test_a_genuine_short_track_between_two_long_ones_survives():
    # phantom_min is the floor, and it is the only floor: 44s is a track. S also
    # clears the veto on its own -- its offsets imply *its* start, 198, not its
    # neighbours' -- which is the difference the rule is built to see.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(refine(100.0, title="A", start=0.0))
    eng.add_probe(refine(200.0, title="A", start=0.0))
    eng.add_probe(refine(204.0, title="S", start=198.0))
    eng.add_probe(refine(244.0, title="S", start=198.0))
    eng.add_probe(refine(248.0, title="C", start=242.0))
    eng.add_probe(refine(300.0, title="C", start=242.0))
    segs, drops = eng.segments()
    assert _titles(segs) == ["A", "S", "C"]
    assert drops == []


def test_unidentified_dropout_inside_one_track_is_absorbed():
    # The sequential path this engine replaced smoothed `A None A`
    # unconditionally (identify._smooth_sequence), but the adaptive rule drops a
    # None run only under phantom_min (20s) while `_target` stops *refining* a
    # None-adjacent interval at precision_none (30s) -- so a dropout that
    # retires anywhere in (20, 30] was permanently unabsorbable, and no further
    # probe could ever narrow it. Measured on the real set: The J.B.'s "These
    # Are the JB's" was reported as three rows around a 22.5s hole. Between two
    # runs of ONE identity a gap is a dropout, not a boundary, so it is judged
    # at the scale the engine actually refines it to.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(probe(185.0, title="A"))  # mid 200
    eng.add_probe(probe(207.5, window=30.0))  # None, mid 222.5
    eng.add_probe(probe(230.0, title="A"))  # mid 245
    eng.add_probe(probe(320.0, title="A"))  # mid 335
    segs, drops = eng.segments()
    assert _titles(segs) == ["A"]
    assert [(d["type"], d["kind"], d["extent"]) for d in drops] == [
        ("phantom_dropped", "dropout", 22.5)
    ]


def test_an_unidentified_stretch_between_two_tracks_is_kept():
    # Same 22.5s hole, different flanks: with A on one side and B on the other
    # it is a genuine unidentified stretch, and the wider floor must not reach
    # it. Only same-identity flanks make a gap a dropout.
    eng = BoundaryEngine(1200.0)
    eng.add_probe(probe(185.0, title="A"))  # mid 200
    eng.add_probe(probe(207.5, window=30.0))  # None, mid 222.5
    eng.add_probe(probe(230.0, title="B"))  # mid 245
    eng.add_probe(probe(320.0, title="B"))  # mid 335
    segs, drops = eng.segments()
    assert _titles(segs) == ["A", None, "B"]
    assert drops == []
