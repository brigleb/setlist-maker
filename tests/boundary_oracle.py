"""Synthetic recordings and a fake Shazam for engine and driver tests.

The oracle answers a probe the way Shazam would: the dominant track in the
window, an offset that reflects where in the song the window landed
(including cut-ins: a track played from its 1:00 mark reports offsets 60s
larger than its recording position implies), and configurable imperfections.
Deterministic for a given seed, which the fold's replay-equality tests rely on.

One detail is load-bearing and was **measured**, not assumed (see the design
spec's Errata): `Shazam.recognize` fingerprints only a *centered*
`FINGERPRINT_SEGMENT` (10s) excerpt of whatever window it is handed. So the
window length does not decide how much audio Shazam hears -- only *which* ten
seconds it hears. Both the identity a straddling probe reports and the offset
it comes back with are therefore properties of the excerpt, not the window,
and this oracle models it that way. Getting this wrong is not cosmetic: a
30s coverage probe and a 12s refine probe of the same track would disagree
about the implied track start by exactly 9s, which is what the engine's
`fingerprint_segment` correction exists to remove.
"""

import random
from dataclasses import dataclass, field

from setlist_maker.boundary import Probe, ProbePlan

FINGERPRINT_SEGMENT = 10.0


def fingerprint_excerpt(t: float, window: float) -> tuple[float, float]:
    """The slice of audio Shazam actually fingerprints for a probe window."""
    if window <= FINGERPRINT_SEGMENT:
        return t, t + window
    lead = (window - FINGERPRINT_SEGMENT) / 2.0
    return t + lead, t + lead + FINGERPRINT_SEGMENT


@dataclass(frozen=True)
class SyntheticTrack:
    artist: str
    title: str
    start: float  # position in the recording
    cut_in: float = 0.0  # seconds into the song at which playback began
    confidence: float = 0.9


@dataclass
class SyntheticSet:
    duration: float
    tracks: list[SyntheticTrack]
    gaps: tuple = ()  # [(start, end)] unidentifiable spans
    seed: int = 0
    offset_jitter: float = 0.0  # +- uniform noise on offsets, seconds
    offset_dropout: float = 0.0  # probability a result carries no offsets
    edge_blur: bool = True  # windows straddling a boundary pick a side by share
    rng: random.Random = field(init=False)

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.tracks = sorted(self.tracks, key=lambda tr: tr.start)

    def boundaries(self) -> list[float]:
        return [tr.start for tr in self.tracks[1:]]

    def _in_gap(self, t: float) -> bool:
        return any(a <= t < b for a, b in self.gaps)

    def track_at(self, t: float) -> SyntheticTrack | None:
        if self._in_gap(t) or not self.tracks or t < self.tracks[0].start:
            return None
        current = None
        for tr in self.tracks:
            if tr.start <= t:
                current = tr
        return current

    def _shares(self, lo: float, hi: float) -> dict:
        """How much of [lo, hi] each track occupies, keyed by track (or None).

        Exact rather than sampled: every point where the answer can change is
        a track start or a gap edge, so walking those partitions the excerpt
        into constant-identity runs."""
        breaks = {lo, hi}
        for tr in self.tracks:
            if lo < tr.start < hi:
                breaks.add(tr.start)
        for a, b in self.gaps:
            for edge in (a, b):
                if lo < edge < hi:
                    breaks.add(edge)
        points = sorted(breaks)
        shares: dict = {}
        span = max(1e-9, hi - lo)
        for left, right in zip(points, points[1:]):
            who = self.track_at((left + right) / 2.0)
            shares[who] = shares.get(who, 0.0) + (right - left) / span
        return shares

    def answer(self, t: float, window: float) -> tuple[dict | None, list[dict] | None]:
        lo, hi = fingerprint_excerpt(t, window)
        shares = self._shares(lo, hi)

        if len(shares) == 1:
            track = next(iter(shares))
        elif self.edge_blur:
            # A straddling excerpt is a weighted coin: the more of a track
            # Shazam hears, the likelier it names it.
            roll, acc, track = self.rng.random(), 0.0, None
            for who, share in shares.items():
                acc += share
                if roll < acc:
                    track = who
                    break
            if track is None:
                track = max(shares, key=shares.get)
        else:
            track = max(shares, key=shares.get)  # whatever dominates

        if track is None:
            return None, None

        result = {
            "artist": track.artist,
            "title": track.title,
            "confidence": track.confidence,
            "coverart_url": None,
            "shazam_url": None,
            "album": None,
        }
        if self.rng.random() < self.offset_dropout:
            return result, None
        # Shazam reports where in the song the *fingerprinted* audio starts.
        offset = (lo - track.start) + track.cut_in
        offset += self.rng.uniform(-self.offset_jitter, self.offset_jitter)
        return result, [{"offset": max(0.0, offset), "timeskew": 0.0}]

    def probe_for(self, plan: ProbePlan) -> Probe:
        result, offsets = self.answer(plan.t, plan.window)
        return Probe(
            t=plan.t,
            window=plan.window,
            purpose=plan.purpose,
            result=result,
            offsets=offsets,
        )


def run_engine(engine, oracle: SyntheticSet, max_probes: int = 2000) -> int:
    n = 0
    while (plan := engine.next_probe()) is not None:
        assert n < max_probes, "engine failed to converge"
        engine.add_probe(oracle.probe_for(plan))
        n += 1
    return n


# A vocabulary whose words are pairwise dissimilar enough that the engine's
# fuzzy identity clustering never merges two of them (worst pair scores 0.83,
# under the 0.9 artist gate). Naming synthetic tracks `Artist 1` / `Track 1`
# instead -- the obvious choice -- is a trap: SequenceMatcher scores
# "artist 1" against "artist 11" at 0.94 and "track 1" against "track 11" at
# 0.93, so both gates pass and a third of the set silently collapses into its
# two-digit neighbours. The engine is right to merge names that similar; a
# fixture just must not hand it any.
_NAMES = """amber basalt cinder dahlia ember fathom girder harbour indigo juniper
kestrel lantern marrow nocturne obsidian pewter quarry rhubarb saffron tundra
umbrella vellum walnut xylem yeoman zephyr almanac bramble crucible driftwood
eggshell furlong gossamer hemlock ironwood jackdaw kelpie limestone mandolin
nutmeg orchard parapet quicksand redwood sandstone thistle undertow vagabond
whetstone""".split()


def synthetic_identity(i: int) -> tuple[str, str]:
    """A distinct (artist, title) pair for synthetic track `i`."""
    return (
        _NAMES[i % len(_NAMES)].title(),
        _NAMES[(i + len(_NAMES) // 2) % len(_NAMES)].title(),
    )


def assert_identities_distinct(tracks) -> None:
    """Fail loudly if a fixture's own names would fuzzy-cluster together.

    Run through the engine's real clustering, so this tracks the thresholds
    rather than restating them."""
    from setlist_maker.boundary import EngineConfig
    from setlist_maker.identify import _assign_cluster, _normalized_key

    cfg = EngineConfig()
    clusters: list[tuple[str, str]] = []
    for tr in tracks:
        _assign_cluster(
            _normalized_key({"artist": tr.artist, "title": tr.title}),
            clusters,
            cfg.title_threshold,
            cfg.artist_threshold,
        )
    assert len(clusters) == len(tracks), (
        f"fixture names collapse under fuzzy clustering: {len(tracks)} tracks "
        f"-> {len(clusters)} clusters; use synthetic_identity()"
    )
