//! Synthetic recordings and a fake Shazam, ported from Python's `tests/boundary_oracle.py`.
//!
//! The oracle answers a probe the way Shazam would: the track that dominates the audio it
//! fingerprints, with an offset saying where in the song that audio is (a track played from
//! its 1:00 mark reports offsets 60s larger), plus configurable imperfections. Shazam
//! fingerprints only the centred 10s of a window, so both the answer and the offset are
//! properties of that excerpt, not of the window. It draws from [`Random`] in Python's
//! order, so a seed gives the run Python's tests saw.

use serde_json::{Value, json};

use super::boundary::{BoundaryEngine, EngineConfig, ProbePlan};
use super::identity::{assign_cluster, normalized_key};
use super::pyrandom::Random;
use crate::model::progress::Probe;

pub const FINGERPRINT_SEGMENT: f64 = 10.0;

/// The slice of audio Shazam fingerprints for a window.
pub fn fingerprint_excerpt(t: f64, window: f64) -> (f64, f64) {
    if window <= FINGERPRINT_SEGMENT {
        return (t, t + window);
    }
    let lead = (window - FINGERPRINT_SEGMENT) / 2.0;
    (t + lead, t + lead + FINGERPRINT_SEGMENT)
}

#[derive(Clone, Debug, PartialEq)]
pub struct SyntheticTrack {
    pub artist: String,
    pub title: String,
    /// Where it starts in the recording.
    pub start: f64,
    /// How far into the song playback began.
    pub cut_in: f64,
    pub confidence: f64,
}

impl SyntheticTrack {
    pub fn new(artist: &str, title: &str, start: f64) -> Self {
        Self {
            artist: artist.into(),
            title: title.into(),
            start,
            cut_in: 0.0,
            confidence: 0.9,
        }
    }
}

pub struct SyntheticSet {
    pub duration: f64,
    pub tracks: Vec<SyntheticTrack>,
    /// Unidentifiable spans.
    pub gaps: Vec<(f64, f64)>,
    /// Plus or minus this much uniform noise on offsets.
    pub offset_jitter: f64,
    /// The chance an answer carries no offsets.
    pub offset_dropout: f64,
    /// A window straddling a boundary picks a side by share, rather than the larger one.
    pub edge_blur: bool,
    /// The chance a probe names a track that isn't playing: Shazam matching a record that
    /// samples the one playing. Each track has one stable impostor, whose offset describes
    /// the real track's position, which is what makes the two implied starts collide.
    pub misid_rate: f64,
    rng: Random,
    impostors: Vec<SyntheticTrack>,
}

impl SyntheticSet {
    pub fn new(duration: f64, mut tracks: Vec<SyntheticTrack>, seed: u64) -> Self {
        // Python's `sorted()` is stable, as is this.
        tracks.sort_by(|a, b| a.start.total_cmp(&b.start));
        Self {
            duration,
            tracks,
            gaps: Vec::new(),
            offset_jitter: 0.0,
            offset_dropout: 0.0,
            edge_blur: true,
            misid_rate: 0.0,
            rng: Random::new(seed),
            impostors: Vec::new(),
        }
    }

    fn impostor(&mut self, track: usize) -> SyntheticTrack {
        if self.impostors.is_empty() {
            assert!(
                2 * self.tracks.len() <= NAMES.len(),
                "misid_rate needs a second name per track"
            );
            let n = self.tracks.len();
            self.impostors = self
                .tracks
                .iter()
                .enumerate()
                .map(|(i, tr)| {
                    let (artist, title) = synthetic_identity(n + i);
                    SyntheticTrack {
                        start: tr.start,
                        cut_in: tr.cut_in,
                        ..SyntheticTrack::new(&artist, &title, 0.0)
                    }
                })
                .collect();
        }
        self.impostors[track].clone()
    }

    pub fn boundaries(&self) -> Vec<f64> {
        self.tracks[1..].iter().map(|t| t.start).collect()
    }

    fn in_gap(&self, t: f64) -> bool {
        self.gaps.iter().any(|&(a, b)| a <= t && t < b)
    }

    /// The index of the track playing at `t`, if one is.
    fn track_at(&self, t: f64) -> Option<usize> {
        if self.in_gap(t) || self.tracks.is_empty() || t < self.tracks[0].start {
            return None;
        }
        self.tracks.iter().rposition(|tr| tr.start <= t)
    }

    /// How much of `[lo, hi]` each track (or no track) takes, in first-heard order. Exact:
    /// the answer can only change at a track start or a gap edge.
    fn shares(&self, lo: f64, hi: f64) -> Vec<(Option<usize>, f64)> {
        let mut points = vec![lo, hi];
        for tr in &self.tracks {
            if lo < tr.start && tr.start < hi {
                points.push(tr.start);
            }
        }
        for &(a, b) in &self.gaps {
            for edge in [a, b] {
                if lo < edge && edge < hi {
                    points.push(edge);
                }
            }
        }
        points.sort_by(f64::total_cmp);
        points.dedup();
        let span = f64::max(1e-9, hi - lo);
        let mut shares: Vec<(Option<usize>, f64)> = Vec::new();
        for w in points.windows(2) {
            let who = self.track_at((w[0] + w[1]) / 2.0);
            let add = (w[1] - w[0]) / span;
            match shares.iter_mut().find(|(k, _)| *k == who) {
                Some((_, s)) => *s += add,
                None => shares.push((who, 0.0 + add)),
            }
        }
        shares
    }

    pub fn answer(&mut self, t: f64, window: f64) -> (Option<Value>, Option<Vec<Value>>) {
        let (lo, hi) = fingerprint_excerpt(t, window);
        let shares = self.shares(lo, hi);
        let largest = || {
            // Python's `max(shares, key=shares.get)`: the first of the largest.
            let mut best = shares[0];
            for &s in &shares[1..] {
                if s.1 > best.1 {
                    best = s;
                }
            }
            best.0
        };
        let track = if shares.len() == 1 {
            shares[0].0
        } else if self.edge_blur {
            // A weighted coin: the more of a track Shazam hears, the likelier it names it.
            let roll = self.rng.random();
            let mut acc = 0.0;
            let mut pick = None;
            for &(who, share) in &shares {
                acc += share;
                if roll < acc {
                    pick = Some(who);
                    break;
                }
            }
            // Landing on unidentified audio falls back to the largest share, as Python's
            // `if track is None` does.
            pick.flatten().or_else(largest)
        } else {
            largest()
        };
        let Some(track) = track else {
            return (None, None);
        };

        let mut named = self.tracks[track].clone();
        if self.misid_rate != 0.0 && self.rng.random() < self.misid_rate {
            named = self.impostor(track);
        }
        let result = json!({
            "artist": named.artist,
            "title": named.title,
            "confidence": named.confidence,
            "coverart_url": null,
            "shazam_url": null,
            "album": null,
        });
        if self.rng.random() < self.offset_dropout {
            return (Some(result), None);
        }
        // Where in the song the fingerprinted audio starts: the real track's, not the name's.
        let real = &self.tracks[track];
        let mut offset = (lo - real.start) + real.cut_in;
        offset += self.rng.uniform(-self.offset_jitter, self.offset_jitter);
        let offset = if offset > 0.0 { offset } else { 0.0 };
        (
            Some(result),
            Some(vec![json!({"offset": offset, "timeskew": 0.0})]),
        )
    }

    pub fn probe_for(&mut self, plan: ProbePlan) -> Probe {
        let (result, offsets) = self.answer(plan.t, plan.window);
        Probe {
            t: plan.t,
            window: plan.window,
            purpose: plan.purpose.as_str().into(),
            result,
            offsets,
        }
    }
}

/// Runs the engine against the oracle until it converges; the number of probes it took.
pub fn run_engine(
    engine: &mut BoundaryEngine,
    oracle: &mut SyntheticSet,
    max_probes: usize,
) -> usize {
    let mut n = 0;
    while let Some(plan) = engine.next_probe() {
        assert!(n < max_probes, "engine failed to converge");
        engine.add_probe(oracle.probe_for(plan));
        n += 1;
    }
    n
}

/// Words dissimilar enough that clustering never merges two (the worst pair scores 0.83,
/// under the 0.9 artist gate). `Artist 1` / `Artist 11` would score 0.94 and merge.
const NAMES: [&str; 49] = [
    "amber",
    "basalt",
    "cinder",
    "dahlia",
    "ember",
    "fathom",
    "girder",
    "harbour",
    "indigo",
    "juniper",
    "kestrel",
    "lantern",
    "marrow",
    "nocturne",
    "obsidian",
    "pewter",
    "quarry",
    "rhubarb",
    "saffron",
    "tundra",
    "umbrella",
    "vellum",
    "walnut",
    "xylem",
    "yeoman",
    "zephyr",
    "almanac",
    "bramble",
    "crucible",
    "driftwood",
    "eggshell",
    "furlong",
    "gossamer",
    "hemlock",
    "ironwood",
    "jackdaw",
    "kelpie",
    "limestone",
    "mandolin",
    "nutmeg",
    "orchard",
    "parapet",
    "quicksand",
    "redwood",
    "sandstone",
    "thistle",
    "undertow",
    "vagabond",
    "whetstone",
];

fn capitalized(word: &str) -> String {
    let mut chars = word.chars();
    chars
        .next()
        .map(|c| c.to_uppercase().chain(chars).collect())
        .unwrap_or_default()
}

/// A distinct (artist, title) for synthetic track `i`.
pub fn synthetic_identity(i: usize) -> (String, String) {
    let n = NAMES.len();
    (
        capitalized(NAMES[i % n]),
        capitalized(NAMES[(i + n / 2) % n]),
    )
}

/// Fails if a fixture's own names would cluster together.
pub fn assert_identities_distinct(tracks: &[SyntheticTrack]) {
    let cfg = EngineConfig::default();
    let mut clusters = Vec::new();
    for tr in tracks {
        let key = normalized_key(&json!({"artist": tr.artist, "title": tr.title}));
        assign_cluster(
            key,
            &mut clusters,
            cfg.title_threshold,
            cfg.artist_threshold,
        );
    }
    assert_eq!(
        clusters.len(),
        tracks.len(),
        "fixture names collapse under fuzzy clustering"
    );
}
