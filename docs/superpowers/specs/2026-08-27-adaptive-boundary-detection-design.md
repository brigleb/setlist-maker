# Adaptive Boundary Detection — Design Spec

**Date:** 2026-08-27
**Status:** Approved design, pre-implementation
**Phase:** 1 of 2 (phase 2, the browser visualizer, gets its own spec later)

## Overview

Today `identify` walks a recording sequentially in 30-second samples: a 4-hour set costs
480 Shazam calls and yields boundaries accurate to ±30s. This spec replaces that with an
**adaptive sampling engine**: sample sparsely, notice where adjacent samples disagree, and
spend the remaining budget refining exactly those disagreement intervals. The algorithm is
**anytime** — interrupt it at any point and the tracklist is complete at whatever precision
has been reached; run it longer and boundaries only get tighter.

Two ideas carry the design:

1. **Noisy binary search on boundaries.** Wherever two probes return different tracks, a
   boundary hides between them. A priority queue always refines the interval with the most
   remaining uncertainty, so total uncertainty shrinks monotonically and stopping early is
   always safe.
2. **Offset-guided prediction.** Shazam's match data (the `matches[].offset` field the
   confidence heuristic already reads past) reports *where within the identified song* the
   sample aligned. A probe at recording time T matching track B at in-track offset O predicts
   B's start at **P = T − O** — one probe locates a boundary that bisection needs four or
   five for. Prediction is verified, never trusted blind, and plain bisection is the built-in
   fallback wherever offsets are missing or inconsistent.

Expected cost on a 4-hour, ~40-track set: ~160 coverage probes + ~1–3 refinement probes per
boundary ≈ **220–280 calls for ±2–5s boundaries**, versus 480 calls for ±30s today.

## Decisions already made (with the user)

- Recordings have hard cuts or very short fades; long beatmatched blends are rare.
- Target precision: **±2–5s** by default, tunable finer or coarser.
- Shortest track that must never be missed: **~2 minutes** → coverage stride **90s**.
- **Adaptive becomes the default** `identify` behavior; `--sequential` preserves the old path.
- Budget controls: precision target, optional wall-clock budget, and Ctrl-C-anytime all
  coexist; the run is interruptible at every point with useful output.
- Live browser visualizer is **phase 2**, but phase 1 emits the structured event log it
  will consume, and the terminal panel shows a compact version of the same state.

## Goals

- Boundaries accurate to the configured target (default: resolved window ≤ 5s) on
  recordings with hard cuts.
- Never miss a track ≥ 2 minutes, **unconditionally** — the guarantee comes from splitting
  geometry, not from offset trust.
- Fewer Shazam calls than the sequential scan on typical material.
- Graceful degradation: every failure of the offset trick degrades to bisection; every
  interruption yields a complete tracklist.
- Resumable across processes, including resuming a half-finished *sequential* run.

## Non-goals

- No change to `chapters`, artwork, corrections, the editors, or output file formats.
- No DSP/audio-analysis dependency (future work).
- No change to the sequential path's behavior beyond it moving behind `--sequential`.
- The browser visualizer (phase 2).

## Architecture

### New module: `setlist_maker/boundary.py`

The engine: interval state, the priority queue, prediction arithmetic, and probe-planning.
**Pure and I/O-free** — it never touches Shazam, files, or the clock. Its interface is
"here is the set of probe results so far; what should be probed next?" plus "fold these
probes into segments." `identify.py` gains an async driver loop that owns the Shazam
client, the inter-call delay, persistence, the panel, and signal handling, mirroring how
the sequential loop is structured today.

This split keeps the algorithm unit-testable with a fake oracle and no network, timing, or
async machinery.

### The interval model

The timeline `[0, D]` is covered by **intervals** whose endpoints are evidence points:
completed probes, plus two virtual endpoints at 0 and D. Each probe contributes an evidence
point at its **window midpoint** (a probe window `[t, t+w]` answering X means "X dominates
this window"; the midpoint is the geometric reading of that claim). Interval kinds:

- **Same-track** (`A … A`): both endpoints identify the same track. May hide a short track.
- **Boundary** (`A … B`, A ≠ B): exactly where refinement spends its budget. `None` counts
  as an identity here (`A … None` is a boundary onto an unidentified region).
- **Edge** (`start … A`, `A … end`): virtual endpoints. Treated as same-track intervals for
  splitting purposes. By convention the first track's final timestamp is 0:00.

Every interval has a **target width** by kind:

| kind | target | default |
|---|---|---|
| same-track / edge | `stride` | 90s |
| boundary (both sides identified) | `precision` | 5s |
| boundary onto `None` | `precision_none` | 30s |

**Priority = width / target.** The scheduler always pops the interval with the highest
ratio; an interval with ratio ≤ 1 is **retired**. Consequences, all intentional:

- The initial state is one interval `[0, D]` with a huge ratio, so early probing is
  breadth-first coverage — midpoint of the file, then quarters, then eighths. Interrupting
  during coverage leaves probes spread uniformly, not clustered in the first hour.
- Same-track intervals split until ≤ 90s **unconditionally**. This is what makes the
  2-minute-track guarantee hold independent of offset quality. (Retiring wide same-track
  intervals early when both endpoints' offsets prove continuous playback is a real probe
  saver, but it makes the guarantee conditional on unvalidated offset behavior — it ships
  **flag-gated and off by default**; see Future work.)
- Boundary intervals refine until ≤ 5s, so total boundary uncertainty shrinks fastest where
  it is largest.
- `None` regions stop at 30s — today's granularity — rather than burning budget bisecting
  an ambient intro Shazam will never name.

### Probe windows

Coverage probes keep the current **30s** window (`SAMPLE_DURATION_MS`) for maximum
identification reliability on unknown material. Refinement probes default to a shorter
window (**12s**, tunable via `--refine-window`): near a known boundary the question is only
"A or B?", and even a weak match adjudicates a two-candidate question. The window length
bounds achievable precision (the oracle is unreliable within roughly a window-length of the
boundary), so the fake-oracle test suite **measures** achievable ε per window size rather
than asserting it; if 12s can't reliably deliver a 5s window, the default precision or
window adjusts before release, in the spec's errata.

### Offset prediction and verification

From any probe of track B at time T with in-track offset O, compute `P = T − O`.

**P is a lower bound on B's start, not an estimate of it.** If B was played from its
beginning, the boundary is at P; if the DJ cut in mid-track (routine in this material), the
true boundary is *later* than P. Two or more probes of the same B with consistent `T − O`
(within a small tolerance, default ±4s) corroborate the same playback timeline and raise
trust; inconsistent values, or a large `timeskew` (tempo-shifted playback), mark the
track's offsets untrusted and route its boundaries to bisection.

**Verification protocol** for a boundary interval `A … B` with trusted prediction P inside
it. Place one refinement probe just **after** P — window chosen so it starts a beat past P
(default: window start = P + 2s) — and expect **B**:

- **Probe answers B:** combined with the lower bound, the boundary lies in `[P, the
  probe's window]`, and the probe's own offset re-predicts P from closer range. Accept the
  (re-)predicted P as the boundary, clamped into that range. Typical cost: **1 probe**.
- **Probe answers A:** cut-in detected — B entered later than its content suggests. The
  prediction is dead; bisect the remaining interval `(probe midpoint, right evidence)`.
- **Probe answers C (a third track):** a hidden track. The probe's evidence point splits
  the interval into `A … C` and `C … B`, both queued. This is also how sub-2-minute
  surprises get caught when they happen to sit at a boundary.
- **Probe answers None:** inconclusive (could be a transition breakdown). Retry once,
  shifted +8s; a second None accepts P flagged low-confidence rather than spending further.

A verification probe answering B **before** P (i.e. contradiction with the lower bound)
means the offsets were wrong; the track's offset trust drops and bisection takes over.

**Bisection fallback** for intervals with no trusted prediction: probe the midpoint,
replace the interval with the surviving half, repeat until ratio ≤ 1 or the per-boundary
probe cap (default 6) is hit. Contradictory answers inside a shrinking interval (A, then B,
then A again) mean the oracle is noisy there — likely a transition zone; the boundary is
recorded at the interval midpoint with low confidence and the interval retires. Every
refinement probe, verification or bisection, feeds its evidence point back into the
interval model — no probe is ever spent without narrowing something.

### Phantom tracks (replacing A-B-A smoothing)

The sequential pipeline smooths isolated outliers passively (`A B A → A`). Adaptive
sampling can do better: it gathers evidence *on demand*. If a single probe claims track C
inside what neighbors call A, the two boundary intervals it spawns get refined like any
others. If C is real, refinement finds its edges. If C was a misidentification, the
refinement probes around it keep answering A, and C's supportable extent collapses.

**Phantom rule:** a track supported by a single probe, whose resolved extent is under
`phantom_min` (default 20s), and whose sample confidence is below the singleton threshold,
is dropped and its neighbors merged. This reuses the meaning (and flag) of
`--singleton-confidence`. Fuzzy identity clustering — the normalization that collapses
remix/feat/edit metadata drift — is reused as-is from the existing dedup code so the same
track under two labels never manufactures a fake boundary.

`--no-smoothing` and sequential-only dedup behavior remain meaningful under
`--sequential`; under adaptive they are accepted with a warning that the adaptive engine
adjudicates outliers by re-probing.

## Anytime behavior, budget, stopping

Internal budget currency is **probe count** (wall-clock is dominated by the inter-call
delay, so time ≈ probes × (delay + probe cost)).

- **Natural termination:** queue empty — every interval retired at its target.
- **`--precision S`:** sets the boundary target width (default 5).
- **`--budget DURATION`** (`45m`, `2h`): converted to a probe allowance; when exhausted,
  stop popping and finalize. Because the scheduler always pops the worst ratio, at
  exhaustion the maximum remaining uncertainty is as small as that budget allowed.
- **Ctrl-C:** first SIGINT finishes the in-flight probe, finalizes outputs, prints the
  summary; a second exits immediately (per-probe persistence means nothing is lost).

Finalization at *any* stopping point is the same code path: fold probes → segments →
boundary estimates (resolved boundaries use their accepted point; unresolved ones use the
interval midpoint, flagged low-confidence) → tracklist.

## Persistence and resume

**Progress format v2:** a JSON object `{"version": 2, "audio_duration": D, "probes":
[...]}` where each probe record is `{t, window, purpose, result, offsets}` (result is the
existing per-sample info dict or null; offsets a compact summary of `matches[].offset` /
`timeskew`). Written after every probe, exactly like today.

**State is a fold over probes.** The interval model and queue are recomputed
deterministically from (probe set, D) — nothing else is serialized, resume is replay, and
the fold function is the same one finalization uses. This keeps the persistence format
dumb and the engine honest (no hidden state that a crash could lose).

**Legacy files:** the current format is a list of `[timestamp, info]` pairs — it already
carries timestamps; only its *resume* semantics were positional. A legacy list is detected
by shape and converted in-memory to v2 probe records (30s windows at their recorded
timestamps). A half-finished sequential run therefore resumes as an adaptive run with a
dense probed prefix — no work discarded, no migration step, no user action.

## CLI surface

- `identify` (default): adaptive engine.
- `--sequential`: the existing scan, unchanged, including its dedup pipeline.
- `--precision S` (default 5), `--budget DURATION` (default: none), `--stride S`
  (default 90; help text states the missed-track tradeoff), `--refine-window S`
  (default 12).
- Existing flags: `--delay`, `--allow-partial`, `--no-summary`, `--no-panel`, `--output`,
  resume behavior — all apply to both modes. `--title-threshold` / `--artist-threshold` /
  `--singleton-confidence` apply in both modes (clustering and the phantom rule);
  `--no-smoothing` is sequential-only (warns under adaptive).

## Observability: event log and panel

The driver appends events to `<base>_events.jsonl` beside the progress file:
`probe_planned`, `probe_result`, `interval_split`, `boundary_predicted`,
`boundary_confirmed`, `cut_in_detected`, `track_discovered`, `phantom_dropped`,
`interval_retired`, `budget_exhausted`, `finalized` — each with timestamps, interval
bounds, and track identities. Phase 2's visualizer is a consumer of this stream (live tail
or replay); nothing in phase 1 depends on it beyond writing it.

The terminal panel (`progress.py`) keeps its fixed four-line geometry — **the panel's
height must never change** (Live redraw corruption; see CLAUDE.md) and every field passes
through the existing one-line/one-cell-wide discipline. Adaptive runs repurpose the same
rail: phase becomes the interval kind being probed, position becomes the probe location,
the progress fraction becomes retired-vs-total uncertainty, and ETA derives from remaining
ratio-weighted work at the configured delay. `format_progress_line()` gets an adaptive
sibling with the same pure-function-of-state testability.

## First implementation step: the offset spike

Before the engine is built, a small throwaway script answers, against one real recording
(plus its known tracklist):

1. Does shazamio populate `matches[].offset` reliably, and with what sign/meaning?
2. Is `T − O` consistent (±ε?) across multiple probes inside one continuously-played track?
3. What happens across a hard cut and near a boundary (straddling windows)?
4. How do 12s windows compare to 30s for identification of already-suspected tracks?

Outcomes gate trust defaults: full trust (verification protocol as specced), partial
(raise the corroboration bar to 2+ consistent probes before predicting), or none (ship
bisection-only; the engine is unchanged, predictions just never fire). The spike's
findings land in this spec's errata section before implementation proceeds past the
engine's pure core.

## Testing strategy

- **Fake oracle:** a synthetic recording description (tracks, starts, cut-in offsets)
  rendered as a callable that answers probes exactly as Shazam would — including
  configurable offset jitter, missing offsets, None zones, wrong-track noise, and
  boundary-straddling ambiguity proportional to window overlap. The engine being pure
  makes this a plain function injection, no network, no async.
- **Properties:** with a clean oracle, every boundary resolves within ε and no track ≥
  stride is missed, at ≤ the probe budget the math predicts; with noise, all boundaries
  still resolve (possibly low-confidence) and no crash/livelock; the anytime invariant —
  truncating the probe sequence at any prefix still folds to a complete tracklist whose
  max uncertainty is monotonically non-increasing in prefix length.
- **Measured ε:** the suite reports achievable precision per refine-window size; the
  default window/precision pair must be supported by measurement, not assertion.
- **Fold determinism:** resume-as-replay equals never-having-stopped, byte-for-byte.
- **Legacy conversion:** an old progress list resumes correctly under adaptive.
- **Driver integration:** `process_single_file` with a mocked
  `identify_sample_with_retry`, both modes, including SIGINT finalization. The autouse
  network guard already enforces that nothing real is called.

## Risks and mitigations

- **Offsets useless in practice** → spike detects it up front; bisection-only mode still
  beats sequential on precision (and roughly matches it on cost).
- **12s windows identify poorly** → measured by spike + fake oracle; fall back to larger
  refine windows at slightly worse ε or more probes.
- **Pathological material** (long blends, mashups, loops): contradiction detection caps
  per-boundary spend and flags low confidence instead of thrashing.
- **Downstream assumptions about dense samples:** the dedup pipeline is bypassed in favor
  of the phantom rule + clustering (shared code where meanings survive); everything below
  `results_to_tracklist()` sees the same shapes it does today.
- **Very short tracks (< 2 min):** knowingly out of the guarantee (user decision);
  mitigated opportunistically by hidden-track discovery during refinement and gap
  suspicion from offset arithmetic.

## Future work (explicitly out of scope for phase 1)

- **Phase 2: browser visualizer** — live timeline of probes, intervals, and collapsing
  uncertainty, consuming the event log; sibling of `web_editor.py`'s loopback server.
- **Continuity retirement:** retire wide same-track intervals when endpoint offsets prove
  continuous playback (`flag: --trust-continuity`), enabling wider default strides. Ships
  off by default until the spike and real-world runs validate offset behavior.
- **Duration corroboration:** predicted end of A (A's start + catalog duration from
  iTunes/Deezer, already queried for artwork) as a second, free boundary prediction.
- **DSP candidate generator:** local novelty detection proposing split points into the
  same queue.
- **Promotion of defaults:** wider stride, finer precision, per the accumulated evidence.

## Errata

### Offset spike findings (2026-08-27)

Run: `scripts/offset_spike.py` against `2026-08-26-Keys-Lounge.mp3` (4:01:08, 60 tracks, with
its sequential tracklist as ground truth). Positions chosen to answer the spec's four
questions: 60/180/300s inside track 1 (*Lou Reed — Sweet Jane*, which starts at 0:00 exactly,
so it pins the absolute bias), 450/500s bracketing its 8:00 cut, and 5850/6000/6150s inside
track 27 (*Dr. Lonnie Smith — I Can't Stand It (Live)*). Every position probed at both 30s and
12s. Raw log in the commit message for `feat(spike)`.

**1. Offsets are populated and trustworthy.** All 12 matching probes carried a single
`matches[].offset`, with `timeskew` between −0.0008 and −0.0028 — three orders of magnitude
inside `timeskew_max = 0.02`, so the skew gate never fires on ordinary material and is doing
its job only against genuinely pitched-up playback.

**2. `T − O` is wrong, and wrong in a way that would have silently killed prediction.**
`Shazam.recognize` does not use shazamio's Python `Converter`; it delegates to `shazamio_core`
(Rust), whose `SearchParams.segment_duration_seconds` (default **10**) documents: *"If the
audio file is longer than this duration, a centered segment of the specified duration is
selected."* So the fingerprinted audio begins `lead = max(0, (window − 10) / 2)` **after** the
probe's window does, and the reported offset describes that point, not the window start:

| window | lead | measured bias vs. plain `T − O` |
|---|---|---|
| 30s (coverage) | 10.0s | +9.6s |
| 12s (refine)   | 1.0s  | +0.6s |

The engine therefore computes `t + lead(window) − offset`, not `t − offset`
(`_probe_start_estimate`). This is not a cosmetic shift. Uncorrected, a coverage probe and a
refine probe of the *same track* disagree by exactly 9.0s = (30−10)/2 − (12−10)/2, which
exceeds `offset_tolerance = 4.0` — so `_trusted_start` would have returned `None` for every
track probed at both window sizes, i.e. for every track the verification protocol actually
reaches. Prediction would have been dead code that never announced itself, and the engine
would have quietly bisected everything.

**3. Corrected, the estimates are excellent.** 30s and 12s probes of the same track agree to
**0.0–0.1s**. Within a track the spread is ≤0.6s over 450s of separation, and that residual is
itself explained by the reported `timeskew ≈ −0.002` (0.002 × 450 ≈ 0.9s). Absolute accuracy:
Sweet Jane, whose true start is 0:00, estimates at 0.0–0.6s; Lonnie Smith estimates at
5787.4–5787.8s against a ground-truth window of (5760, 5790]. `offset_tolerance = 4.0` is
consequently generous rather than tight, which is the right side to err on.

**4. 12s refine windows are supported.** Every position that matched at 30s also matched at
12s (8/8). The one failure — 8:20, just past a hard cut — failed at *both* window sizes, so
there is no evidence the shorter window identifies worse. `refine_window = 12` ships as
specced.

**Correction (2026-08-28):** that 8:20 failure was *not* a non-match, and not shazamio's. It
was `shazam_client.py`'s own album lookup: `track.get("metadata", [{}])[0]` takes the key's
own value whenever the key exists, so Shazam's routine `"metadata": []` indexes an empty list
and raises `IndexError`. That escaped into `identify_sample_with_retry`'s blanket
`except Exception`, which **discards a successful identification** and reports the sample as
unidentified. Measured on the first real adaptive run: 10 of 273 calls, nine of them
consecutive across 7:46-11:00, which erased *The Brains - Money Changes Everything* from the
set entirely **and** made the engine spend those nine probes bisecting a region it had been
told was unidentifiable. A tenth erased *Wings - Love Is Strange*. Both identify correctly
once the lookup indexes defensively (`_album_name`), both reporting `album=None` -- which
confirms the empty-metadata shape as the trigger. The bug predates adaptive sampling and
affected `--sequential` identically. It is also the sharpest argument for `call_log.py`
recording exception *type*: the log is what made a silent 3.7% loss visible at all.

**5. Consequence the spec got wrong: a 30s coverage window buys no extra reliability.**
Because only a centered 10s excerpt is ever fingerprinted, Shazam sees exactly 10 seconds
whether it is handed 12s or 30s — the window only chooses *which* 10 seconds. The spec's
rationale for keeping 30s coverage probes ("maximum identification reliability on unknown
material") does not hold. `coverage_window` stays 30 in phase 1 regardless, because it is the
sequential path's window and changing it would move evidence points for no measured gain; the
saving on offer is decode/export time, not match quality. Flagged as future work.

**6. Midpoint attribution is confirmed correct.** The centered excerpt's own midpoint is
`t + (w − 10)/2 + 5 = t + w/2` for any `w ≥ 10` (and trivially for `w < 10`), so `Probe.mid`
is exactly the fingerprint centroid — the interval model's geometric reading is right. But the
evidence *extent* is 10s for every window size, so a probe cannot localise a boundary better
than ±5s no matter how it is sized. That is what bounds achievable precision, and it is why
`precision = 5.0` sits exactly at the edge of what a single probe can resolve.

**7. `min_corroboration` stays 2** (partial trust), despite the measurements supporting full
trust on precision grounds. The residual risk prediction guards against is not offset
*precision* — measured at ±0.4s — but offset *provenance*: one probe that matched the wrong
track yields a confident-looking P from a single number. A second agreeing probe is what
rejects that, and coverage at a 90s stride supplies one for free on any track long enough to
matter. Promoting to 1 would only help tracks too short for the stride guarantee anyway.


### Implementation deviations (2026-08-28)

Recorded here because each one departs from what this spec or the implementation
plan says, and every one is load-bearing.

**A. Edge intervals are targeted before `None` intervals.** `_target` tests "does this
interval touch a virtual endpoint" *ahead of* "is either side unidentified", so a recording
that opens or closes on unidentifiable audio is covered at the `stride` rather than bisected
to `precision_none` against a sentinel that was never evidence of anything. This matches the
spec's own table ("same-track / edge → stride"); the plan's inline code had the two branches
the other way round and contradicted its own test.

**B. Coverage precedes boundary retirement (`_needs_coverage`).** The spec defends the
"never miss a track ≥ 2 minutes" guarantee by splitting *same-track* intervals to the stride
unconditionally, and explicitly notes that retiring wide intervals on offset trust would make
the guarantee conditional. The same hazard applies to **boundary** intervals, which the spec
did not consider: their target is `precision`, so a confirmed prediction retired an `A…B`
interval that was still hundreds of seconds wide, leaving that span unprobed. Measured on the
synthetic 4-hour set, **five whole tracks vanished** into such intervals. Every interval of
every kind is now split to the stride before any boundary consideration applies. Prediction
buys precision, never permission to skip coverage.

**C. Verification is aligned to the fingerprinted excerpt, and `verify_lead` is now 0.0.**
The spec places the verification probe's *window* at `P + 2s`. Given finding 2 above, that
means Shazam actually listens to `[P+3, P+13]` and answers "B" for any cut-in under 8s —
i.e. a mistaken prediction lands 3s outside the 5s boundary target. The probe is now offset so
the *excerpt* starts at `P + verify_lead`, and acceptance tests the excerpt's start rather than
the window's (a coverage probe's differ by 10s, so a window test would let one vouch for a
boundary it never listened to). With `verify_lead = 0.0` the excerpt is `[P, P+10]`, so a
cut-in is missed only when it is under `fingerprint_segment / 2` = 5s — exactly `precision`.
This is why `precision = 5.0` survives as the default rather than being relaxed: it is the
matched pair to a 10s fingerprint, not an arbitrary target. The knob remains, trading
cut-in sensitivity against tolerance for noise in P.

**D. `_near_existing` compares excerpts, not window starts.** A 30s coverage probe and a 12s
refine probe sharing a start time hear audio 9s apart, so a window-start test is wrong in both
directions: it blocks a refine probe that would hear entirely new audio, and permits two probes
that would hear the same ten seconds. Measured, the first of these stalled bisection at an 18s
interval and left a 7.4s boundary error.

**E. Per-coverage-gap refine cap replaces the per-boundary probe cap.** The spec caps
refinement at 6 probes "per boundary", but every probe becomes an evidence point that splits
its interval, so "probes inside this interval" is always zero. The cap is enforced over the
span between the two nearest coverage probes instead (`max_refines_per_gap = 12`), which is
the region a thrashing transition zone actually occupies.

**F. Coverage probes snap to a stride grid.** `_grid_mid` picks the nearest stride multiple
strictly inside an interval, so full coverage tiles a file in `duration / stride` probes.
Blind midpoint halving costs up to 2× that (14400/2ᵏ first dips under 90 at 56.25s spacing).

**Measured after A–F**, against the synthetic oracle (which models the centered-excerpt
behaviour above), 40 seeds per condition:

| condition | median | p90 | max | over 5s | tracks missed |
|---|---|---|---|---|---|
| clean | 0.06s | 1.33s | 4.44s | 0 of 576 | 0 |
| jitter + 30% offset dropout + blurred edges | 0.74s | 3.88s | 7.01s | 3.6% | 0 |

At the 4-hour, 40-track scale, 20 of 20 seeds hold every boundary within `precision` inside the
plan's probe budget, at roughly 240 calls against the sequential scan's 480 — i.e. **half the
Shazam calls for boundaries about an order of magnitude tighter**, matching this spec's
predicted 220–280.

### Scope note: the call log

`--call-log` is wired into the adaptive driver as well as the sequential one. The
implementation plan routed it only through `process_single_file`; since adaptive is the
default, that would have left the log — which is on by default so that a throttling question
has data waiting — empty for every ordinary run. `total` is the engine's live estimate of
probes, there being no fixed count.
### Contradiction collapse (2026-08-28)

**G. The spec's contradiction rule was never implemented, and the first design for it was
wrong.** "Bisection fallback" promised that contradictory answers inside a shrinking interval
(A, then B, then A again) would retire as one low-confidence boundary. Nothing did that, and
adaptive sampling made the omission *visible*: the scheduler probes hardest exactly where two
tracks meet, so the fold sees Shazam flip-flopping and reports each flip as a track. Measured
on `2026-08-26-Keys-Lounge.mp3` (317 probes):

| zone | folded as | truth |
|---|---|---|
| 3:44 | Us3 7.1s / Grant Green 22.8s / Us3 5.6s / Grant Green 611.9s | one cut, Boz Scaggs → Grant Green |
| 1:06 | Roy Ayers / **B.I.G. 16.3s** / Herb Alpert 334.7s | one cut, Roy Ayers → Herb Alpert |

Both are **semantic**, not noise: Us3 sample Blue Note records and Grant Green is Blue Note;
"Hypnotize" samples "Rise". Expect sampled/sampling pairs. Note also that 1:06 is *not* an
alternation — three distinct identities in a row — so the spec's A-B-A-B rule alone could
never have reached it.

**H. Geometry alone is a track-length guillotine.** The obvious rule — drop a pinned run whose
*measured* extent is under `phantom_min` — convicts all three real phantoms, and is a strict
regression. Swept against the oracle (30 seeds per cell, engine run to convergence, short
track placed uniformly between two long ones):

| real track length | deleted, clean | deleted, noisy | kept by the code before this |
|---|---|---|---|
| 12s | 19/19 | 23/24 | all |
| 15s | 21/21 | 24/25 | all |
| 18s | 19/21 | 17/25 | all |
| 20s | 12/21 | 15/25 | all |
| 25s | 0/21 | 2/24 | all |

It also takes `test_confident_single_probe_track_survives`' own fixture once that recording is
probed to convergence rather than by four hand-placed probes: the flanks turn `resolved`, and
the rule deletes the very short track lesson #7 exists to protect. Geometry may only *open* the
question.

**I. Offsets answer it, and the two readings that work are both relative.** `_misattributed`
convicts a run when its own probes' implied starts (per **run**, never `_trusted_start` of the
cluster — that would average a phantom together with real sightings of the same title elsewhere)
either:

- **spread further than the run is long** (+ `offset_tolerance`). The second Us3 run spreads
  49.0s across 5.6s of audio: two probes 2.8s apart cannot imply starts 49s apart. The bound
  must be the run's own extent, not a constant — 10 of the file's 51 multi-probe runs
  legitimately spread past 4s, *Girl You Need a Change of Mind* by 455s over a 462s track, so
  any fixed tolerance either misses the phantom or convicts a sixth of the set; or
- **collide with a better-supported neighbouring run's**, within `offset_tolerance`. Two
  identities cannot both begin at the same instant. Measured: "Hypnotize" implies 3973.3 and
  "Rise" implies 3973.3 — the same number to 0.1s — and the first Us3 run lands 1.4s off Grant
  Green's. Comparing `(probes, extent)` is what makes the test one-directional; without it the
  phantom and the real track convict each other symmetrically.

Measured effect of the veto: on the real file it convicts **exactly** the three phantoms and
nothing else, at every collide tolerance from 1.5s to 5s and every spread rule tried. Against
the oracle it cuts false deletions of genuine 12–20s tracks from 34/161 to 5/161, and from
5/29 to **0/29** in the A-C-A geometry above. Absolute spread (4s) instead of relative gives
31/161 — the relative bound is doing most of the work.

**J. The alternation rule needs the veto more than the sliver rule does.** `_alternation_zones`
detects the spec's shape — two *overlapping* returns of different identities, i.e. A-B-A-B,
where a return is one identity's consecutive pair separated by an excursion under the `stride`
— and raises the extent ceiling to the stride inside such a zone. On geometry alone that
deletes a real record in the **rewind/reload**: a DJ pulls a record back, both stretches are
genuine, both are under 90s, and 162 seconds of a real track is re-attributed to the winner.
With the veto the oracle acquits **40 of 40** rewinds. Two further guards: the zone's own
best-supported identity is never collapsed (on the real file the *correct* track clears
`phantom_min` by 2.8s, less than the measured p90 boundary error of 3.88s), and nothing at or
above the stride is ever dropped.

**K. The flank test cannot borrow `--precision`.** A run is only collapsible when both bounding
boundaries are pinned within `min(_target(...), fingerprint_segment / 2)`. Reading the plain
target would mean `--precision 30` — a knob whose purpose is to spend *fewer* probes — silently
widened the rule's blast radius sixfold. `fingerprint_segment / 2` is the physical floor from
Errata 6: a probe cannot localise a boundary better than ±5s at any window size.

**L. `A None A` was a regression against the path this replaced.** `_smooth_sequence` absorbs a
lone `None` between identical neighbours unconditionally; the adaptive rule dropped a `None` run
only under `phantom_min` (20s) while `_target` stops refining a `None`-adjacent interval at
`precision_none` (30s) — so a dropout retiring in (20, 30] was permanently unabsorbable. The
real file reported The J.B.'s as three rows around a 22.5s hole no further probe could narrow.
A gap between two runs of **one** identity is now judged at `precision_none`; `_needs_coverage`
still splits everything wider than the stride, so the ≥ 2-minute guarantee is untouched.

**Measured result** on the real file: 61 segments / 55 identified rows → **55 / 50**. The five
rows are 3 phantom rows dropped (*Hypnotize*, *Tukka Yoot's Riddim* ×2 — the only two titles
that disappear from the set) plus 2 duplicate rows *merged* (Grant Green and The J.B.'s each
reported twice around a phantom). Every remaining sub-60s row is deliberate: *Wings* (37.3s,
real) and two 12.7s runs flanked by unidentified audio, which the `None` gate spares. Across
all 317 prefixes exactly two titles appear and later vanish — both phantoms — and none
flickers: the collapse is not monotone in track presence, because refinement is what licenses
it, but it is stable.

**Coverage note:** `tests/boundary_oracle.py` cannot generate this failure mode — its answers
always name a track actually present in the excerpt, so no oracle seed produces a
misidentification, and the property suite in `test_boundary_properties.py` is green on this
feature *vacuously*. The regression net is `tests/fixtures/*.json`, two verbatim slices of the
real run. Extending the oracle with a `misid_rate` that answers with a track absent from the
excerpt is the missing capability.
