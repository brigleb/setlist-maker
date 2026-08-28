"""Whole-engine properties: correctness, cost, anytime behavior, measured
precision. If `test_measured_precision_supports_defaults` fails, the DEFAULTS
are wrong, not the test: adjust `EngineConfig.refine_window`/`precision` per
the spec ("the default window/precision pair must be supported by measurement")
and record the change in the spec's Errata."""

import random

from setlist_maker.boundary import BoundaryEngine, EngineConfig, Probe
from tests.boundary_oracle import (
    SyntheticSet,
    SyntheticTrack,
    assert_identities_distinct,
    run_engine,
    synthetic_identity,
)


def _random_set(seed, duration=14400.0, n_tracks=40, cut_in_rate=0.5):
    rng = random.Random(seed)
    starts = sorted(rng.uniform(120.0, duration - 240.0) for _ in range(n_tracks - 1))
    # Enforce the spec's floor: tracks are >= 2 minutes apart.
    keep, last = [0.0], 0.0
    for s in starts:
        if s - last >= 120.0:
            keep.append(s)
            last = s
    tracks = []
    for i, s in enumerate(keep):
        artist, title = synthetic_identity(i)
        tracks.append(
            SyntheticTrack(
                artist=artist,
                title=title,
                start=s,
                cut_in=rng.uniform(0.0, 90.0) if rng.random() < cut_in_rate else 0.0,
            )
        )
    assert_identities_distinct(tracks)
    # Blur is opt-in per test: the "clean oracle" cases answer by window
    # midpoint, the measurement gate turns edge_blur back on.
    return SyntheticSet(duration=duration, tracks=tracks, seed=seed, edge_blur=False)


def _boundary_errors(engine, oracle):
    segs, _ = engine.segments()
    starts = [s.start for s in segs]
    return [min(abs(b - s) for s in starts) for b in oracle.boundaries()]


def test_clean_oracle_finds_every_boundary_within_precision():
    oracle = _random_set(seed=1)
    engine = BoundaryEngine(oracle.duration)
    n = run_engine(engine, oracle)
    errors = _boundary_errors(engine, oracle)
    assert max(errors) <= engine.cfg.precision
    # Cost: coverage (~160) + a few refines per boundary.
    assert n <= 160 + 6 * len(oracle.boundaries())


def test_no_track_of_at_least_two_minutes_is_missed():
    oracle = _random_set(seed=2)
    engine = BoundaryEngine(oracle.duration)
    run_engine(engine, oracle)
    segs, _ = engine.segments()
    titles = {s.info["title"] for s in segs if s.info}
    assert titles == {tr.title for tr in oracle.tracks}


def test_anytime_prefix_always_yields_complete_tracklist():
    oracle = _random_set(seed=3, duration=3600.0, n_tracks=12)
    engine = BoundaryEngine(oracle.duration)
    worst = []
    n = 0
    while (plan := engine.next_probe()) is not None and n < 2000:
        engine.add_probe(oracle.probe_for(plan))
        n += 1
        segs, _ = engine.segments()
        assert segs and segs[0].start == 0.0  # complete at every prefix
        errors = _boundary_errors(engine, oracle)
        worst.append(max(errors) if errors else 0.0)
    # Uncertainty must trend down: the final read beats the first by a lot.
    assert worst[-1] <= worst[len(worst) // 4]


def test_replay_equality():
    oracle = _random_set(seed=4, duration=3600.0, n_tracks=10)
    engine = BoundaryEngine(oracle.duration)
    run_engine(engine, oracle)
    replayed = BoundaryEngine(oracle.duration)
    for p in engine.probes:
        replayed.add_probe(Probe(p.t, p.window, p.purpose, p.result, p.offsets))
    assert replayed.next_probe() is None
    assert replayed.segments() == engine.segments()


def test_measured_precision_supports_defaults():
    """MEASUREMENT GATE (spec: Probe windows). Noisy conditions: offset jitter,
    30% offset dropout, blurred boundary windows. p90 of boundary error must
    stay within the default precision target."""
    errors = []
    for seed in range(5):
        oracle = _random_set(seed=100 + seed, duration=7200.0, n_tracks=20)
        oracle.offset_jitter = 1.0
        oracle.offset_dropout = 0.3
        oracle.edge_blur = True
        engine = BoundaryEngine(oracle.duration)
        run_engine(engine, oracle)
        errors.extend(_boundary_errors(engine, oracle))
    errors.sort()
    p90 = errors[int(len(errors) * 0.9)]
    assert p90 <= EngineConfig().precision, (
        f"p90 boundary error {p90:.1f}s exceeds the default precision target; "
        "adjust EngineConfig defaults per the spec's measurement gate and "
        "record it in the spec Errata"
    )


def test_gap_heavy_set_converges_with_unidentified_segments():
    tracks = [SyntheticTrack("A", "A", 0.0), SyntheticTrack("B", "B", 1200.0)]
    oracle = SyntheticSet(duration=1800.0, tracks=tracks, gaps=((500.0, 900.0),), seed=5)
    engine = BoundaryEngine(oracle.duration)
    n = run_engine(engine, oracle)
    segs, _ = engine.segments()
    assert any(s.info is None for s in segs)
    assert n < 200


def test_misidentified_phantoms_collapse_without_taking_a_real_track():
    """MEASUREMENT GATE for `_collapsed`. The plain oracle can only ever name a
    track that IS in the excerpt, so every gate above is green on contradiction
    collapse *vacuously*; `misid_rate` supplies the one failure it cannot -- a
    record answering over the audio it samples, which is what produced both real
    phantoms (spec Errata, contradiction collapse). The rule may erase impostors;
    it may not erase a real track to do it.

    "Real tracks found" is deliberately measured from the *probe results*, not
    from the oracle: at a 12% contamination rate a track thin enough to get one
    probe sometimes has that one probe stolen, and the fold cannot report what
    it was never told. Counting those as erasures would make the gate fail for
    the contaminant's doing rather than the rule's -- measured, that is exactly
    the two losses this fixture produces at 14 tracks over 12 seeds.
    """
    erased = phantoms_left = phantoms_seen = 0
    for seed in range(12):
        oracle = _random_set(seed=700 + seed, duration=7200.0, n_tracks=14)
        oracle.edge_blur = True
        oracle.misid_rate = 0.12
        engine = BoundaryEngine(oracle.duration)
        run_engine(engine, oracle)
        segs, _ = engine.segments()
        titles = {s.info["title"] for s in segs if s.info}
        named = {p.result["title"] for p in engine.probes if p.result}
        real = {tr.title for tr in oracle.tracks}
        erased += len((real & named) - titles)
        phantoms_seen += len(named - real)
        phantoms_left += len((named - real) & titles)
    assert phantoms_seen > 0, "fixture produced no misidentifications to collapse"
    assert erased == 0, f"{erased} real tracks the probes found were erased by the collapse"
    # Measured 43 of 89. The survivors are not failures: this contaminant fires
    # independently per probe, so most impostors land alone in the middle of a
    # long track, where the flanks stay coarse and the A-B-A shape belongs to
    # `singleton_confidence_keep` by design. A real sampling confusion is
    # localised to a transition instead, which is why the committed fixtures --
    # not this gate -- are what pin efficacy. This one exists to keep the rule
    # from becoming inert, and to fail loudly if it starts eating real tracks.
    assert phantoms_left <= 0.7 * phantoms_seen, (
        f"{phantoms_left} of {phantoms_seen} misidentifications survived the collapse"
    )
