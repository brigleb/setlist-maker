//! The adaptive boundary engine, ported from Python's `boundary.py`.
//!
//! It consumes completed [`Probe`]s and answers "what should be probed next?"
//! ([`BoundaryEngine::next_probe`]) and "what does the evidence say the recording contains?"
//! ([`BoundaryEngine::segments`]). All state is a deterministic fold over the probe sequence:
//! replaying the same probes in the same order rebuilds the same engine, which is what makes
//! resume "load the probe list and replay it".
//!
//! The port is literal, down to the order floats are added in, because M2's parity harness
//! requires Hindsight to fold every saved run to Python's bytes. Python's own docstrings carry
//! the measurements behind each rule; the short versions are kept here.
//!
//! - **Intervals.** The timeline is cut at each probe's window midpoint, plus the two ends of
//!   the recording. Each interval's target width depends on what's either side of it: the
//!   stride (90s) for one track or an edge, the precision (5s) for a boundary between two
//!   tracks, 30s beside unidentified audio. Priority is width over target, so early probes
//!   cover the whole file and later ones tighten the worst boundary: stopping after any
//!   number of probes leaves the least uncertainty that many could buy.
//! - **Coverage first.** An interval wider than the stride is split for coverage whatever its
//!   ends claim, since a whole track could hide in it. Prediction buys precision, never
//!   permission to skip coverage: without this, five whole tracks vanished from a 4-hour set.
//! - **Offsets.** Shazam fingerprints the centred 10s of whatever window it's given, so a
//!   probe's offset describes audio `(window - 10) / 2` into the window. A track's start
//!   predicted from two or more agreeing probes is a lower bound (the DJ may have cut in
//!   mid-song), so it's only accepted once a probe *heard* the track just after it.
//! - **Contradiction collapse.** Probing hardest at transitions means seeing Shazam change its
//!   mind there. A run is dropped only when it's pinned between two identified runs, too short
//!   to be a track (or inside an A-B-A-B alternation) and convicted by its own offsets.

use std::collections::HashMap;

use indexmap::IndexMap;
use serde::Serialize;
use serde_json::Value;

use super::identity::{Key, assign_cluster, find_cluster, normalized_key};
use super::py;
use crate::model::progress::Probe;

/// Tunable knobs. The app and CLI use the defaults; the tests vary them.
#[derive(Clone, Debug, PartialEq)]
pub struct EngineConfig {
    /// Most unprobed span between two sightings of one track.
    pub stride: f64,
    /// A boundary's target width.
    pub precision: f64,
    /// The target width when one side is unidentified.
    pub precision_none: f64,
    pub coverage_window: f64,
    pub refine_window: f64,
    /// How much of a window Shazam fingerprints, centred (shazamio-core's default). A probe's
    /// offset describes audio starting `(window - this) / 2` into the window.
    pub fingerprint_segment: f64,
    /// The widest spread among one track's implied starts that still counts as agreement.
    pub offset_tolerance: f64,
    /// Beyond this, playback was tempo-shifted and the offset means nothing.
    pub timeskew_max: f64,
    /// Probes needed before offsets are trusted.
    pub min_corroboration: usize,
    /// How far after a predicted start the verification probe's fingerprinted audio starts.
    /// At 0 a missed cut-in is bounded by `fingerprint_segment / 2`, which is `precision`.
    pub verify_lead: f64,
    /// Refine probes a transition zone may absorb between two coverage probes.
    pub max_refines_per_gap: usize,
    /// The least extent a one-probe track needs, unless it's confident.
    pub phantom_min: f64,
    pub singleton_confidence_keep: f64,
    pub title_threshold: f64,
    pub artist_threshold: f64,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            stride: 90.0,
            precision: 5.0,
            precision_none: 30.0,
            coverage_window: 30.0,
            refine_window: 12.0,
            fingerprint_segment: 10.0,
            offset_tolerance: 4.0,
            timeskew_max: 0.02,
            min_corroboration: 2,
            verify_lead: 0.0,
            max_refines_per_gap: 12,
            phantom_min: 20.0,
            singleton_confidence_keep: 0.6,
            title_threshold: 0.85,
            artist_threshold: 0.9,
        }
    }
}

/// What a point on the timeline is evidence of.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Identity {
    /// The recording's start, a virtual endpoint that asserts nothing.
    Start,
    /// The recording's end.
    End,
    /// A probe Shazam had no answer for.
    Unidentified,
    /// A track: an index into the engine's clusters.
    Track(usize),
}

impl Identity {
    fn is_track(self) -> bool {
        matches!(self, Identity::Track(_))
    }
}

/// A point on the timeline with a known identity: a probe's window midpoint, or an end.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Evidence {
    mid: f64,
    identity: Identity,
    /// The probe's index, or `None` for the two ends.
    probe: Option<usize>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Purpose {
    Coverage,
    Refine,
}

impl Purpose {
    pub fn as_str(self) -> &'static str {
        match self {
            Purpose::Coverage => "coverage",
            Purpose::Refine => "refine",
        }
    }
}

/// What the engine wants probed next.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ProbePlan {
    pub t: f64,
    pub window: f64,
    pub purpose: Purpose,
}

/// How well a segment's start is known.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Confidence {
    /// Within the target width.
    Resolved,
    /// The best guess so far.
    Coarse,
}

/// One entry of the folded tracklist.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Segment {
    pub start: f64,
    /// The cluster's most confident Shazam answer, or `None` for unidentified audio.
    pub info: Option<Value>,
    /// How well `start` is known.
    pub confidence: Confidence,
}

/// What an `add_probe` changed, for `<set>_events.jsonl`. Names are the cluster's best
/// answer's, as stored (so `null` when it had none).
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    ProbeResult {
        t: f64,
        window: f64,
        purpose: String,
        artist: Value,
        title: Value,
        confidence: Value,
    },
    TrackDiscovered {
        artist: Value,
        title: Value,
        at: f64,
    },
    CutInDetected {
        at: f64,
        predicted: f64,
    },
    IntervalSplit {
        at: f64,
    },
    IntervalRetired {
        left: f64,
        right: f64,
    },
    BoundaryConfirmed {
        start: f64,
        artist: Value,
        title: Value,
    },
    BoundaryPredicted {
        predicted_start: f64,
        artist: Value,
        title: Value,
    },
}

/// A run [`BoundaryEngine::segments`] left out, and why.
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Dropped {
    PhantomDropped {
        kind: PhantomKind,
        /// Only for a [`PhantomKind::Track`].
        #[serde(flatten, skip_serializing_if = "Option::is_none")]
        name: Option<Name>,
        start: f64,
        extent: f64,
    },
    ContradictionCollapsed {
        kind: CollapseKind,
        reason: Conviction,
        artist: Value,
        title: Value,
        start: f64,
        extent: f64,
        /// Only inside an alternation zone.
        #[serde(flatten, skip_serializing_if = "Option::is_none")]
        zone: Option<Zone>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PhantomKind {
    /// An unidentified gap inside one track.
    Dropout,
    /// A short unidentified gap between two tracks.
    Gap,
    /// A one-probe, low-confidence track too short to be one.
    Track,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CollapseKind {
    /// Inside an alternation zone and shorter than the stride.
    Thrash,
    /// Shorter than `phantom_min`.
    Sliver,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Conviction {
    /// The run's own probes disagree on its start by more than the run is long.
    Incoherent,
    /// The run implies the same start as a better-supported neighbour.
    Collide,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Name {
    pub artist: Value,
    pub title: Value,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Zone {
    pub winner_artist: Value,
    pub winner_title: Value,
    pub zone: [f64; 2],
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Status {
    Resolved,
    Capped,
    Retired,
    Active,
}

pub struct BoundaryEngine {
    duration: f64,
    cfg: EngineConfig,
    probes: Vec<Probe>,
    /// Real probes only, sorted by midpoint.
    evidence: Vec<Evidence>,
    /// Parallel to `probes`.
    identities: Vec<Identity>,
    /// Parallel to `probes`: each one's implied track start, which depends only on the probe.
    estimates: Vec<Option<f64>>,
    clusters: Vec<Key>,
    /// Each cluster's most confident answer.
    meta: HashMap<usize, Value>,
}

/// A non-empty JSON object: Python's `if not result` for anything a writer produces.
fn answered(result: Option<&Value>) -> Option<&Value> {
    result.filter(|r| matches!(r, Value::Object(o) if !o.is_empty()))
}

/// `round(mid, 3)` as a map key. `-0.0` and `0.0` are one key, as in a Python dict.
fn key3(x: f64) -> u64 {
    let r = py::round_to(x, 3);
    (if r == 0.0 { 0.0 } else { r }).to_bits()
}

fn round1(x: f64) -> f64 {
    py::round_to(x, 1)
}

/// Python's `(a, b) > (c, d)` for an int then a float.
fn support_greater(x: (usize, f64), y: (usize, f64)) -> bool {
    if x.0 != y.0 {
        return x.0 > y.0;
    }
    x.1 > y.1
}

impl BoundaryEngine {
    pub fn new(duration: f64, config: EngineConfig) -> Self {
        Self {
            duration,
            cfg: config,
            probes: Vec::new(),
            evidence: Vec::new(),
            identities: Vec::new(),
            estimates: Vec::new(),
            clusters: Vec::new(),
            meta: HashMap::new(),
        }
    }

    pub fn duration(&self) -> f64 {
        self.duration
    }

    pub fn config(&self) -> &EngineConfig {
        &self.cfg
    }

    /// Every probe folded so far, in fold order.
    pub fn probes(&self) -> &[Probe] {
        &self.probes
    }

    // ---- identity ----------------------------------------------------------

    /// The cluster an index belongs to: the first equal key. Only differs from the index when
    /// a threshold above 1 lets equal keys start separate clusters.
    fn canonical(&self, i: usize) -> usize {
        self.clusters
            .iter()
            .position(|k| *k == self.clusters[i])
            .unwrap_or(i)
    }

    fn identify(&mut self, result: Option<&Value>) -> Identity {
        let Some(result) = answered(result) else {
            return Identity::Unidentified;
        };
        let i = assign_cluster(
            normalized_key(result),
            &mut self.clusters,
            self.cfg.title_threshold,
            self.cfg.artist_threshold,
        );
        let c = self.canonical(i);
        let confidence = py::number_or_zero(result.get("confidence"));
        let better = match self.meta.get(&c) {
            None => true,
            Some(best) => confidence > py::number_or_zero(best.get("confidence")),
        };
        if better {
            self.meta.insert(c, result.clone());
        }
        Identity::Track(c)
    }

    fn meta(&self, identity: Identity) -> Option<&Value> {
        match identity {
            Identity::Track(c) => self.meta.get(&c),
            _ => None,
        }
    }

    fn meta_field(&self, identity: Identity, field: &str) -> Value {
        self.meta(identity)
            .and_then(|m| m.get(field))
            .cloned()
            .unwrap_or(Value::Null)
    }

    // ---- fold --------------------------------------------------------------

    fn statuses(&self) -> IndexMap<(u64, u64), Status> {
        let mut out = IndexMap::new();
        for (left, right) in self.pairs() {
            out.insert((key3(left.mid), key3(right.mid)), self.status(left, right));
        }
        out
    }

    /// Folds one completed probe into the model and reports what changed.
    ///
    /// Events come from snapshotting the intervals' statuses and each cluster's prediction
    /// before and after the insert, so there's no incremental bookkeeping to drift.
    pub fn add_probe(&mut self, probe: Probe) -> Vec<Event> {
        let before = self.statuses();
        let pred_before: Vec<Option<f64>> = (0..self.clusters.len())
            .map(|i| self.trusted_start(Identity::Track(self.canonical(i))))
            .collect();
        let mid = probe.mid();
        let enclosing = self.enclosing(mid);

        let is_new = answered(probe.result.as_ref()).is_some_and(|r| {
            let key = normalized_key(r);
            find_cluster(
                &key,
                &self.clusters,
                self.cfg.title_threshold,
                self.cfg.artist_threshold,
            )
            .is_none()
                && !self.clusters.contains(&key)
        });

        let identity = self.identify(probe.result.as_ref());
        let index = self.probes.len();
        let at = self.evidence.partition_point(|e| e.mid < mid);
        self.evidence.insert(
            at,
            Evidence {
                mid,
                identity,
                probe: Some(index),
            },
        );
        self.estimates.push(self.probe_start_estimate(&probe));
        self.identities.push(identity);
        let confidence = probe
            .result
            .as_ref()
            .and_then(|r| answered(Some(r)))
            .and_then(|r| r.get("confidence"))
            .cloned()
            .unwrap_or(Value::Null);
        let mut events = vec![Event::ProbeResult {
            t: round1(probe.t),
            window: probe.window,
            purpose: probe.purpose.clone(),
            artist: self.meta_field(identity, "artist"),
            title: self.meta_field(identity, "title"),
            confidence,
        }];
        self.probes.push(probe);

        if is_new && identity.is_track() {
            events.push(Event::TrackDiscovered {
                artist: self.meta_field(identity, "artist"),
                title: self.meta_field(identity, "title"),
                at: round1(mid),
            });
        }
        if let Some((left, right)) = enclosing
            && self.is_boundary(left, right)
            && identity == left.identity
            && let Some(p_start) = self.trusted_start(right.identity)
            && self.excerpt_start(&self.probes[index]) >= p_start - 0.5
        {
            events.push(Event::CutInDetected {
                at: round1(mid),
                predicted: round1(p_start),
            });
        }

        let after = self.statuses();
        if after.keys().any(|k| !before.contains_key(k)) {
            events.push(Event::IntervalSplit { at: round1(mid) });
        }
        for (&(lo, hi), &status) in &after {
            let prev = before.get(&(lo, hi)).copied();
            if status == Status::Retired && prev != Some(Status::Retired) {
                events.push(Event::IntervalRetired {
                    left: round1(f64::from_bits(lo)),
                    right: round1(f64::from_bits(hi)),
                });
            } else if status == Status::Resolved && prev != Some(Status::Resolved) {
                // The first interval with these rounded ends, as Python's `next()` finds it.
                let pair = self
                    .pairs()
                    .find(|(l, r)| key3(l.mid) == lo && key3(r.mid) == hi);
                if let Some((l, r)) = pair
                    && let Some(p_start) = self.resolved_by_prediction(l, r)
                {
                    events.push(Event::BoundaryConfirmed {
                        start: round1(p_start),
                        artist: self.meta_field(r.identity, "artist"),
                        title: self.meta_field(r.identity, "title"),
                    });
                }
            }
        }
        for i in 0..self.clusters.len() {
            let key = Identity::Track(self.canonical(i));
            let now = self.trusted_start(key);
            if let Some(now) = now
                && pred_before.get(i).copied().flatten().is_none()
            {
                events.push(Event::BoundaryPredicted {
                    predicted_start: round1(now),
                    artist: self.meta_field(key, "artist"),
                    title: self.meta_field(key, "title"),
                });
            }
        }
        events
    }

    // ---- interval model ----------------------------------------------------

    fn points(&self) -> Vec<Evidence> {
        let mut out = Vec::with_capacity(self.evidence.len() + 2);
        out.push(Evidence {
            mid: 0.0,
            identity: Identity::Start,
            probe: None,
        });
        out.extend_from_slice(&self.evidence);
        out.push(Evidence {
            mid: self.duration,
            identity: Identity::End,
            probe: None,
        });
        out
    }

    fn pairs(&self) -> impl Iterator<Item = (Evidence, Evidence)> {
        let points = self.points();
        (1..points.len()).map(move |i| (points[i - 1], points[i]))
    }

    fn target(&self, left: Evidence, right: Evidence) -> f64 {
        let (li, ri) = (left.identity, right.identity);
        // An edge splits like one track: the virtual endpoint asserts nothing, so the only
        // job is coverage. Tested before the unidentified case on purpose.
        if li == Identity::Start || ri == Identity::End {
            return self.cfg.stride;
        }
        if li == Identity::Unidentified || ri == Identity::Unidentified {
            return self.cfg.precision_none;
        }
        if li == ri {
            return self.cfg.stride;
        }
        self.cfg.precision
    }

    fn is_boundary(&self, left: Evidence, right: Evidence) -> bool {
        left.identity.is_track() && right.identity.is_track() && left.identity != right.identity
    }

    fn enclosing(&self, mid: f64) -> Option<(Evidence, Evidence)> {
        self.pairs().find(|(l, r)| l.mid <= mid && mid < r.mid)
    }

    // ---- offset prediction -------------------------------------------------

    /// How far into a window the audio Shazam hears begins.
    fn fingerprint_lead(&self, window: f64) -> f64 {
        py::max(0.0, (window - self.cfg.fingerprint_segment) / 2.0)
    }

    /// Where a probe's fingerprinted audio starts, in recording time.
    fn excerpt_start(&self, probe: &Probe) -> f64 {
        probe.t + self.fingerprint_lead(probe.window)
    }

    /// The track start a probe implies: a lower bound, since a track the DJ cut into
    /// mid-song implies a start earlier than the real boundary. Not plain `t - offset`: the
    /// offset describes the centred excerpt, `lead` seconds into the window. Uncorrected, a
    /// 30s and a 12s probe of one track disagree by exactly 9s, more than
    /// `offset_tolerance`, and prediction would silently never fire.
    fn probe_start_estimate(&self, probe: &Probe) -> Option<f64> {
        let offsets = probe.offsets.as_ref().filter(|o| !o.is_empty())?;
        let lead = self.fingerprint_lead(probe.window);
        let candidates: Vec<f64> = offsets
            .iter()
            .filter_map(|m| {
                let offset = py::number(m.get("offset"))?;
                (py::number_or_zero(m.get("timeskew")).abs() <= self.cfg.timeskew_max)
                    .then_some(probe.t + lead - offset)
            })
            .collect();
        py::median(&candidates)
    }

    /// A cluster's predicted start, if enough of its probes agree on it.
    fn trusted_start(&self, key: Identity) -> Option<f64> {
        let starts: Vec<f64> = self
            .identities
            .iter()
            .zip(&self.estimates)
            .filter_map(|(ident, est)| (*ident == key).then_some(*est).flatten())
            .collect();
        if starts.len() < self.cfg.min_corroboration {
            return None;
        }
        let spread = py::max_of(starts.iter().copied())? - py::min_of(starts.iter().copied())?;
        if spread > self.cfg.offset_tolerance {
            return None;
        }
        py::median(&starts)
    }

    /// The accepted boundary for an A..B interval: B's trusted start inside it, and some B
    /// probe whose fingerprinted audio began within `[P - 0.5, P + precision]`. A cut-in
    /// answers A instead, which moves the interval's left edge past P.
    fn resolved_by_prediction(&self, left: Evidence, right: Evidence) -> Option<f64> {
        if !self.is_boundary(left, right) {
            return None;
        }
        let key = right.identity;
        let p_start = self.trusted_start(key)?;
        if !(left.mid < p_start && p_start < right.mid) {
            return None;
        }
        self.probes
            .iter()
            .zip(&self.identities)
            .any(|(p, ident)| {
                let start = self.excerpt_start(p);
                *ident == key && p_start - 0.5 <= start && start <= p_start + self.cfg.precision
            })
            .then_some(p_start)
    }

    // ---- scheduling --------------------------------------------------------

    /// The most valuable probe to run next, or `None` once converged.
    pub fn next_probe(&self) -> Option<ProbePlan> {
        if self.evidence.is_empty() {
            if self.duration <= 1.0 {
                return None;
            }
            let window = py::min(self.cfg.coverage_window, self.duration);
            let t = py::max(0.0, self.duration / 2.0 - window / 2.0);
            return Some(ProbePlan {
                t,
                window,
                purpose: Purpose::Coverage,
            });
        }
        let mut best: Option<(f64, ProbePlan)> = None;
        for (left, right) in self.pairs() {
            let ratio = (right.mid - left.mid) / self.target(left, right);
            if ratio <= 1.0 {
                continue;
            }
            if self.is_boundary(left, right) && !self.needs_coverage(left, right) {
                if self.resolved_by_prediction(left, right).is_some() {
                    continue;
                }
                if self.capped(left, right) {
                    continue;
                }
            }
            let Some(plan) = self.plan_for(left, right) else {
                continue;
            };
            if best.is_none_or(|(b, _)| ratio > b + 1e-9) {
                best = Some((ratio, plan));
            }
        }
        best.map(|(_, plan)| plan)
    }

    fn plan_for(&self, left: Evidence, right: Evidence) -> Option<ProbePlan> {
        let cfg = &self.cfg;
        let width = right.mid - left.mid;
        let (window, purpose, mid);
        if self.needs_coverage(left, right) {
            // Too wide to characterise, whatever its ends claim.
            (window, purpose) = (cfg.coverage_window, Purpose::Coverage);
            mid = self.grid_mid(left, right);
        } else if self.is_boundary(left, right) {
            if let Some(p_start) = self.trusted_start(right.identity)
                && left.mid < p_start
                && p_start < right.mid
            {
                // Offset the window so the fingerprinted excerpt lands where verification
                // wants it, then clamp: an early P could plan a negative start.
                let t = p_start + cfg.verify_lead - self.fingerprint_lead(cfg.refine_window);
                let t = py::min(
                    py::max(t, 0.0),
                    py::max(0.0, self.duration - cfg.refine_window),
                );
                if t + cfg.refine_window / 2.0 < right.mid - 0.25
                    && !self.near_existing(t, cfg.refine_window)
                {
                    return Some(ProbePlan {
                        t,
                        window: cfg.refine_window,
                        purpose: Purpose::Refine,
                    });
                }
            }
            (window, purpose) = (cfg.refine_window, Purpose::Refine);
            mid = (left.mid + right.mid) / 2.0;
        } else {
            // Beside unidentified audio: hunting an identity, not a boundary.
            window = if width > cfg.coverage_window * 1.5 {
                cfg.coverage_window
            } else {
                cfg.refine_window
            };
            purpose = Purpose::Coverage;
            mid = (left.mid + right.mid) / 2.0;
        }
        let t = mid - window / 2.0;
        let t = py::min(py::max(t, 0.0), py::max(0.0, self.duration - window));
        let centre = t + window / 2.0;
        if !(left.mid + 0.25 < centre && centre < right.mid - 0.25) {
            return None; // an unprobeable sliver: clamping pushed the window out
        }
        if self.near_existing(t, window) {
            return None;
        }
        Some(ProbePlan { t, window, purpose })
    }

    /// The nearest stride multiple strictly inside the interval, so full coverage tiles the
    /// file in `duration / stride` probes where blind halving can cost twice that.
    fn grid_mid(&self, left: Evidence, right: Evidence) -> f64 {
        let s = self.cfg.stride;
        let mid = (left.mid + right.mid) / 2.0;
        let mut g = (mid / s).round_ties_even() * s;
        if g <= left.mid + 0.5 {
            g += s;
        }
        if g >= right.mid - 0.5 {
            g -= s;
        }
        if !(left.mid + 0.5 < g && g < right.mid - 0.5) {
            return mid;
        }
        g
    }

    /// Would this probe fingerprint audio an earlier probe already heard? Compares excerpts,
    /// not window starts, which differ by 9s between a coverage and a refine probe.
    fn near_existing(&self, t: f64, window: f64) -> bool {
        let start = t + self.fingerprint_lead(window);
        self.probes
            .iter()
            .any(|p| (self.excerpt_start(p) - start).abs() < 0.5)
    }

    /// The span between the nearest coverage (or end) evidence either side.
    fn coverage_gap(&self, left: Evidence, right: Evidence) -> (f64, f64) {
        let (mut lo, mut hi) = (0.0, self.duration);
        for e in self.points() {
            let anchored = e
                .probe
                .is_none_or(|i| self.probes[i].purpose == Purpose::Coverage.as_str());
            if anchored && e.mid <= left.mid {
                lo = e.mid;
            }
            if anchored && e.mid >= right.mid {
                hi = e.mid;
                break;
            }
        }
        (lo, hi)
    }

    /// The thrash guard: a zone that keeps contradicting itself stops absorbing probes once
    /// its coverage gap has used up the cap.
    fn capped(&self, left: Evidence, right: Evidence) -> bool {
        let (lo, hi) = self.coverage_gap(left, right);
        let n = self
            .probes
            .iter()
            .filter(|p| p.purpose == Purpose::Refine.as_str() && lo <= p.mid() && p.mid() <= hi)
            .count();
        n >= self.cfg.max_refines_per_gap
    }

    /// Wider than the stride, so a whole track could still be hiding here. Checked ahead of
    /// every boundary consideration.
    fn needs_coverage(&self, left: Evidence, right: Evidence) -> bool {
        right.mid - left.mid > self.cfg.stride
    }

    fn status(&self, left: Evidence, right: Evidence) -> Status {
        if self.is_boundary(left, right) && !self.needs_coverage(left, right) {
            if self.resolved_by_prediction(left, right).is_some() {
                return Status::Resolved;
            }
            if self.capped(left, right) {
                return Status::Capped;
            }
        }
        let ratio = (right.mid - left.mid) / self.target(left, right);
        if ratio <= 1.0 {
            Status::Retired
        } else {
            Status::Active
        }
    }

    // ---- progress metrics --------------------------------------------------

    fn active_pairs(&self) -> Vec<(Evidence, Evidence, f64)> {
        self.pairs()
            .filter(|&(l, r)| self.status(l, r) == Status::Active && self.plan_for(l, r).is_some())
            .map(|(l, r)| (l, r, (r.mid - l.mid) / self.target(l, r)))
            .collect()
    }

    /// The worst interval's width over its target; 0 once converged.
    pub fn max_ratio(&self) -> f64 {
        py::max_of(self.active_pairs().into_iter().map(|(_, _, ratio)| ratio)).unwrap_or(0.0)
    }

    /// The widest boundary still being narrowed, in seconds.
    pub fn max_boundary_width(&self) -> f64 {
        py::max_of(
            self.active_pairs()
                .into_iter()
                .filter(|&(l, r, _)| self.is_boundary(l, r))
                .map(|(l, r, _)| r.mid - l.mid),
        )
        .unwrap_or(0.0)
    }

    /// A rough count of the probes left before convergence.
    pub fn estimated_probes_remaining(&self) -> u64 {
        if self.evidence.is_empty() {
            return u64::from(self.duration > 1.0);
        }
        let at_least_one = |x: f64| py::max(1.0, x) as u64;
        let mut total = 0;
        for (left, right, ratio) in self.active_pairs() {
            let width = right.mid - left.mid;
            total += if self.needs_coverage(left, right) {
                at_least_one((width / self.cfg.stride).ceil() - 1.0)
            } else if self.is_boundary(left, right)
                && self
                    .trusted_start(right.identity)
                    .is_some_and(|p| left.mid < p && p < right.mid)
            {
                1
            } else {
                at_least_one(ratio.log2().ceil())
            };
        }
        total
    }

    // ---- contradiction collapse --------------------------------------------

    /// Each probe in the run's own implied start: a property of the probe, not the cluster,
    /// which would average a phantom with the real sightings elsewhere.
    fn run_estimates(&self, run: &[Evidence]) -> Vec<f64> {
        run.iter()
            .filter_map(|ev| ev.probe.and_then(|i| self.estimates[i]))
            .collect()
    }

    /// Positive evidence that run `i`'s matches are another track's audio: its own probes
    /// disagree on the start by more than the run is long (incoherent), or it implies a
    /// better-supported neighbour's start (collide). A run with no offsets is never
    /// convicted.
    fn misattributed(
        &self,
        runs: &[Vec<Evidence>],
        extents: &[f64],
        i: usize,
    ) -> Option<Conviction> {
        let own = self.run_estimates(&runs[i]);
        if own.is_empty() {
            return None;
        }
        if own.len() > 1
            && py::max_of(own.iter().copied())? - py::min_of(own.iter().copied())?
                > extents[i] + self.cfg.offset_tolerance
        {
            return Some(Conviction::Incoherent);
        }
        let mine = py::median(&own)?;
        for j in [i.checked_sub(1), Some(i + 1)].into_iter().flatten() {
            if j >= runs.len() || runs[j][0].identity == Identity::Unidentified {
                continue;
            }
            let theirs = self.run_estimates(&runs[j]);
            let Some(theirs) = py::median(&theirs) else {
                continue;
            };
            if (mine - theirs).abs() > self.cfg.offset_tolerance {
                continue;
            }
            if support_greater((runs[j].len(), extents[j]), (runs[i].len(), extents[i])) {
                return Some(Conviction::Collide);
            }
        }
        None
    }

    /// `(lo, hi, winner)` for every span where two identities take turns. A return is one
    /// identity's consecutive pair of runs no more than a stride apart (A-B-A); a zone needs
    /// two overlapping returns of different identities (A-B-A-B). An unidentified run clears
    /// every open return. The winner is the most probes, then the most extent, then the
    /// earliest, so the fold stays deterministic.
    fn alternation_zones(
        &self,
        runs: &[Vec<Evidence>],
        starts: &[f64],
        ends: &[f64],
    ) -> Vec<(usize, usize, Identity)> {
        let mut returns = Vec::new();
        let mut last: HashMap<Identity, usize> = HashMap::new();
        for (i, run) in runs.iter().enumerate() {
            let ident = run[0].identity;
            if ident == Identity::Unidentified {
                last.clear();
                continue;
            }
            if let Some(&prev) = last.get(&ident)
                && i - prev >= 2
                && starts[i] - ends[prev] <= self.cfg.stride
            {
                returns.push((prev, i, ident));
            }
            last.insert(ident, i);
        }

        let mut comps: Vec<(usize, usize, Vec<Identity>)> = Vec::new();
        for (lo, hi, ident) in returns {
            match comps.last_mut() {
                Some(c) if lo <= c.1 => {
                    c.1 = c.1.max(hi);
                    c.2.push(ident);
                }
                _ => comps.push((lo, hi, vec![ident])),
            }
        }

        let mut zones = Vec::new();
        for (lo, hi, idents) in comps {
            let distinct = idents.iter().any(|i| *i != idents[0]);
            if idents.len() < 2 || !distinct {
                continue;
            }
            let mut tally: IndexMap<Identity, (usize, f64, usize)> = IndexMap::new();
            for k in lo..=hi {
                let ident = runs[k][0].identity;
                if ident == Identity::Unidentified {
                    continue;
                }
                let (probes, extent, first) = tally.get(&ident).copied().unwrap_or((0, 0.0, k));
                tally.insert(
                    ident,
                    (probes + runs[k].len(), extent + ends[k] - starts[k], first),
                );
            }
            // Python's `max()`: the first of the greatest by (probes, extent, -first).
            let mut winner: Option<(Identity, (usize, f64, usize))> = None;
            for (&ident, &score) in &tally {
                let greater = winner.is_none_or(|(_, best)| {
                    if score.0 != best.0 {
                        score.0 > best.0
                    } else if score.1 != best.1 {
                        score.1 > best.1
                    } else {
                        score.2 < best.2
                    }
                });
                if greater {
                    winner = Some((ident, score));
                }
            }
            if let Some((winner, _)) = winner {
                zones.push((lo, hi, winner));
            }
        }
        zones
    }

    /// Run index to audit event, for each run the evidence contradicts. Three gates: pinned
    /// between two identified runs by measured boundaries; too small to be a track (under
    /// `phantom_min`, or under the stride inside an alternation zone); and convicted by its
    /// own offsets. A zone's winner is never collapsed.
    fn collapsed(
        &self,
        runs: &[Vec<Evidence>],
        starts: &[f64],
        ends: &[f64],
        measured: &[bool],
    ) -> HashMap<usize, Dropped> {
        let cfg = &self.cfg;
        let extents: Vec<f64> = starts.iter().zip(ends).map(|(s, e)| e - s).collect();
        let mut winner_at: HashMap<usize, Identity> = HashMap::new();
        let mut span_at: HashMap<usize, (usize, usize)> = HashMap::new();
        for (lo, hi, winner) in self.alternation_zones(runs, starts, ends) {
            for k in lo..=hi {
                winner_at.insert(k, winner);
                span_at.insert(k, (lo, hi));
            }
        }

        let mut out = HashMap::new();
        for (i, run) in runs.iter().enumerate() {
            let ident = run[0].identity;
            if ident == Identity::Unidentified || !(0 < i && i + 1 < runs.len()) {
                continue;
            }
            if runs[i - 1][0].identity == Identity::Unidentified
                || runs[i + 1][0].identity == Identity::Unidentified
            {
                continue;
            }
            if !(measured[i - 1] && measured[i]) {
                continue;
            }
            if winner_at.get(&i) == Some(&ident) {
                continue;
            }
            let kind = if winner_at.contains_key(&i) && extents[i] < cfg.stride {
                CollapseKind::Thrash
            } else if extents[i] < cfg.phantom_min {
                CollapseKind::Sliver
            } else {
                continue;
            };
            let Some(reason) = self.misattributed(runs, &extents, i) else {
                continue;
            };
            let zone = winner_at.get(&i).map(|&w| {
                let (lo, hi) = span_at[&i];
                Zone {
                    winner_artist: self.meta_field(w, "artist"),
                    winner_title: self.meta_field(w, "title"),
                    zone: [round1(starts[lo]), round1(ends[hi])],
                }
            });
            out.insert(
                i,
                Dropped::ContradictionCollapsed {
                    kind,
                    reason,
                    artist: self.meta_field(ident, "artist"),
                    title: self.meta_field(ident, "title"),
                    start: round1(starts[i]),
                    extent: round1(extents[i]),
                    zone,
                },
            );
        }
        out
    }

    // ---- finalization ------------------------------------------------------

    fn runs(&self) -> Vec<Vec<Evidence>> {
        let mut runs: Vec<Vec<Evidence>> = Vec::new();
        for &ev in &self.evidence {
            match runs.last_mut() {
                Some(run) if run[run.len() - 1].identity == ev.identity => run.push(ev),
                _ => runs.push(vec![ev]),
            }
        }
        runs
    }

    /// Folds the evidence into a tracklist, with what was left out and why. Callable after
    /// any number of probes, which makes every way of stopping one code path.
    pub fn segments(&self) -> (Vec<Segment>, Vec<Dropped>) {
        let cfg = &self.cfg;
        let runs = self.runs();
        if runs.is_empty() {
            let segments = if self.duration > 0.0 {
                vec![Segment {
                    start: 0.0,
                    info: None,
                    confidence: Confidence::Coarse,
                }]
            } else {
                Vec::new()
            };
            return (segments, Vec::new());
        }

        // The boundary between each run and the next. `measured` is a stricter,
        // precision-independent read: could a probe have pinned it?
        let mut bounds: Vec<(f64, Confidence)> = Vec::new();
        let mut measured = Vec::new();
        for pair in runs.windows(2) {
            let (left, right) = (pair[0][pair[0].len() - 1], pair[1][0]);
            let p_start = self.resolved_by_prediction(left, right);
            let gap = right.mid - left.mid;
            match p_start {
                Some(p) => bounds.push((p, Confidence::Resolved)),
                None => {
                    let conf = if gap <= self.target(left, right) {
                        Confidence::Resolved
                    } else {
                        Confidence::Coarse
                    };
                    bounds.push(((left.mid + right.mid) / 2.0, conf));
                }
            }
            measured.push(
                p_start.is_some()
                    || gap <= py::min(self.target(left, right), cfg.fingerprint_segment / 2.0),
            );
        }

        let mut starts = vec![0.0];
        starts.extend(bounds.iter().map(|b| b.0));
        let mut ends: Vec<f64> = bounds.iter().map(|b| b.0).collect();
        ends.push(self.duration);
        let mut collapsed = self.collapsed(&runs, &starts, &ends, &measured);

        // Phantoms. Confidence is the run's own probe's, not its cluster's (#7).
        let mut keep = Vec::new();
        let mut drops = Vec::new();
        for (i, run) in runs.iter().enumerate() {
            let ident = run[0].identity;
            let span = ends[i] - starts[i];
            if let Some(event) = collapsed.remove(&i) {
                drops.push(event);
                continue;
            }
            if ident == Identity::Unidentified {
                // Between two runs of one track, a gap is a dropout inside it, judged at
                // `precision_none` because that's where refining it stops.
                let inside_one_track = 0 < i
                    && i + 1 < runs.len()
                    && runs[i - 1][0].identity != Identity::Unidentified
                    && runs[i - 1][0].identity == runs[i + 1][0].identity;
                let floor = if inside_one_track {
                    cfg.precision_none
                } else {
                    cfg.phantom_min
                };
                if span < floor {
                    drops.push(Dropped::PhantomDropped {
                        kind: if inside_one_track {
                            PhantomKind::Dropout
                        } else {
                            PhantomKind::Gap
                        },
                        name: None,
                        start: round1(starts[i]),
                        extent: round1(span),
                    });
                    continue;
                }
            }
            if ident != Identity::Unidentified && run.len() == 1 && span < cfg.phantom_min {
                let result = run[0]
                    .probe
                    .and_then(|p| answered(self.probes[p].result.as_ref()));
                let conf = py::number_or_zero(result.and_then(|r| r.get("confidence")));
                if conf < cfg.singleton_confidence_keep {
                    drops.push(Dropped::PhantomDropped {
                        kind: PhantomKind::Track,
                        name: Some(Name {
                            artist: self.meta_field(ident, "artist"),
                            title: self.meta_field(ident, "title"),
                        }),
                        start: round1(starts[i]),
                        extent: round1(span),
                    });
                    continue;
                }
            }
            keep.push(i);
        }

        let mut out = Vec::new();
        let mut prev_kept: Option<usize> = None;
        for i in keep {
            let ident = runs[i][0].identity;
            let info = self.meta(ident).cloned();
            match prev_kept {
                None => out.push(Segment {
                    start: 0.0,
                    info,
                    confidence: Confidence::Resolved,
                }),
                // The same track continues across a dropped blip.
                Some(p) if ident == runs[p][0].identity => {}
                Some(p) if i == p + 1 => {
                    let (start, confidence) = bounds[p];
                    out.push(Segment {
                        start,
                        info,
                        confidence,
                    });
                }
                // Dropped runs in between: the boundary goes in the middle of them.
                Some(p) => out.push(Segment {
                    start: (bounds[p].0 + bounds[i - 1].0) / 2.0,
                    info,
                    confidence: Confidence::Coarse,
                }),
            }
            prev_kept = Some(i);
        }
        if out.is_empty() {
            out.push(Segment {
                start: 0.0,
                info: None,
                confidence: Confidence::Coarse,
            });
        }
        (out, drops)
    }

    /// (boundaries between distinct identified runs, how many of them are at target).
    pub fn boundary_stats(&self) -> (usize, usize) {
        let runs = self.runs();
        let (mut found, mut at_target) = (0, 0);
        for pair in runs.windows(2) {
            let (left, right) = (pair[0][pair[0].len() - 1], pair[1][0]);
            if !self.is_boundary(left, right) {
                continue;
            }
            found += 1;
            if self.resolved_by_prediction(left, right).is_some()
                || right.mid - left.mid <= self.target(left, right)
            {
                at_target += 1;
            }
        }
        (found, at_target)
    }
}

#[cfg(test)]
mod tests;
