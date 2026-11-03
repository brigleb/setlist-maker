# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Setlist Maker is a Python CLI tool that generates tracklists from DJ sets or long audio recordings by slicing them into 30-second samples and identifying each via Shazam.

## Development Commands

```bash
# Install for development: .venv from uv.lock, with the dev extras
uv sync --extra dev

# Run the CLI
uv run setlist-maker <audio_file_or_directory>

# Run as module
uv run python -m setlist_maker <audio_file_or_directory>

# Lint with ruff
uv run ruff check .
uv run ruff format .

# Run tests (~15s). A global `pytest` can't import the deps; use uv run or .venv/bin/pytest.
uv run pytest
```

**System dependency:** Requires ffmpeg installed (`brew install ffmpeg` / `apt install ffmpeg`).
Its bundled `ffplay` also powers the editor's audio preview (macOS only).

## Architecture

CLI application with the following modules:

### `setlist_maker/cli.py` - Argparse entry point & command handlers
- **Entry point:** `main()` with subcommand routing (`identify`, `chapters`)
- **Backward compatible:** Running without subcommand defaults to `identify` behavior
- **Thin layer:** Owns argparse setup and the `cmd_identify` / `cmd_chapters` handlers,
  delegating real work to the modules below.

### `setlist_maker/help_format.py` - Colorized `--help`

- **ColorHelpParser:** an `ArgumentParser` whose `print_help()` paints argparse's *rendered*
  output rather than re-authoring the help in markup. The top-level epilog is the manual —
  workflow, commands, every flag, examples — and duplicating it in markup would mean
  maintaining it twice, so this stays a post-pass and the epilog keeps sourcing every default
  from its canonical constant. Overriding `print_help` catches both routes into it (the
  no-argument branch in `main()` and argparse's own `-h` action), and children built by
  `add_subparsers()` are `type(self)`, so `identify -h` / `chapters -h` inherit it with no
  wiring — which is why the constructor signature must stay untouched.
- **colorize_help():** pure `str -> rich.Text`, so the rules unit-test by reading spans back
  off the result with no console, no pty and no `isatty`, exactly as `progress.render_panel()`
  does. Styling is **purely additive** — strip the escapes and the characters are argparse's,
  byte for byte — which is what a test asserts, because a regex that ate a flag off the screen
  would be a silently worse help page.
- Each rule is anchored on something argparse's formatter guarantees, never on prose: headings
  are short punctuation-free lines at the left margin, metavars are uppercase tokens
  *immediately following a flag* (a bare-uppercase rule paints "DJ", "MP3" and "JSON" out of
  the description), and the flag rule's `(?<![\w-])` lookbehind is what keeps `0-1`,
  `single-sample` and the `A B A -> A` arrow from turning green. Subcommand names are
  highlighted only at a listing row's start or straight after the program name — every other
  `identify` in the epilog is prose. Command names come from the live `add_subparsers()`
  `choices` dict, so a new subcommand needs no list updated here.
- Palette is `progress.py`'s (`green` accent, `grey62` muted, bold structure) so the help
  screen and the run panel read as one program.
- Whether to colorize is left to rich's `Console`, not a hand-rolled `isatty()` check, which
  is what gets `NO_COLOR`, `TERM=dumb` and redirection handled the same way as everywhere else
  rich is used. `soft_wrap=True` is load-bearing: the epilog's columns are hand-aligned past
  rich's 80-column non-tty default, and without it a piped `--help` would be re-wrapped into
  something different from what argparse wrote.

### `setlist_maker/audio.py` - Audio discovery, loading, slicing
- **get_audio_file():** Validates a single path is an existing, supported audio file
- **load_audio() / slice_audio():** Uses `pydub` to load and slice into 30-second chunks
- **Decode-completeness guard:** `load_audio()` cross-checks the decoded length against
  ffprobe's reported duration (`probe_duration_seconds()` → `verify_decode_complete()`) and
  raises `TruncatedAudioError` on a large shortfall — the signature of a file still being
  written or synced (e.g. iCloud) when read, where the OS reports the full size but only part
  has materialized. Prevents silently producing a partial tracklist. Bypass with `identify
  --allow-partial`. Tunables: `DECODE_SHORTFALL_REL_TOLERANCE`, `DECODE_SHORTFALL_MIN_GAP_SECONDS`
- `SAMPLE_DURATION_MS = 30000`; `AUDIO_EXTENSIONS` lives in `__init__.py`

### `setlist_maker/shazam_client.py` - Shazam recognition
- **identify_sample_with_retry():** Wraps `shazamio` with exponential-backoff retry for rate limits.
  **The rate-limit branch is dead code** — it tests `"429"/"too many"/"rate"` against `str(e)`, but a
  real Shazam 429 either raises nothing at all or raises `FailedDecodeJson("Failed to decode json")`,
  which matches none of them. Left in place deliberately while `call_log.py` measures a baseline;
  see that section before changing it.
- **on_error hook:** every failure here collapses to a `None` return that the pipeline cannot tell
  from audio Shazam does not know. `on_error(exc)` hands the exception out before that distinction
  is lost, so the call log can record its *type*. Mirrors the existing `on_backoff` callback.
- **estimate_confidence():** Heuristic match-confidence proxy (match alignment + corroboration),
  attached to each result as `confidence` and used by the dedup pipeline
- Constants: `MAX_RETRIES`, `INITIAL_BACKOFF`

### `setlist_maker/identify.py` - Sequential identification pipeline
- **No longer the default.** `identify` samples adaptively now (`boundary.py` + `adaptive.py`);
  everything below is what `--sequential` selects. The two drivers share `apply`-side code
  rather than duplicating it: `finalize_outputs()` (summary + markdown + JSON sidecar, extracted
  verbatim from this file's tail) and `results_to_tracklist()`. The latter grew a keyword-only
  `deduplicate=False`, which the adaptive driver passes: `segments()` already yields one entry
  per track, and every track appearing exactly once is precisely what the singleton filter
  drops — running the dedup pipeline over it would empty the tracklist.
- **Resume guard:** an adaptive run writes a progress *object*, not a list of samples. This path
  refuses one by shape rather than letting `len()` count its keys and resume from a nonsense
  index.
- **process_single_file():** Orchestrates slicing → recognition → dedup → summary → output
- **deduplicate_tracklist():** Fuzzy-clusters matches (normalizes remix/feat/edit tags so metadata
  drift for one track collapses), smooths isolated single-sample outliers (A B A / A None A → A),
  drops low-confidence singletons while keeping confident short tracks, then collapses consecutive
  identical tracks. Smoothing is **confidence-aware**: it only absorbs an outlier whose own sample
  confidence is below `singleton_confidence_keep`, so a real short track sandwiched between two
  longer ones survives for the singleton filter to adjudicate instead of being erased first (#7).
  A `None` dropout has no confidence to defend it and is always smoothed. The gate reads the
  *sample's* confidence, not its cluster's, so a shaky blip is still absorbed when the same track
  is confidently detected elsewhere. Tunables live in `DedupConfig` (defaults: `SIMILARITY_THRESHOLD`,
  `ARTIST_SIMILARITY_THRESHOLD`, `SINGLETON_CONFIDENCE_KEEP`) and are exposed as `identify`
  flags: `--title-threshold`, `--artist-threshold`, `--singleton-confidence`, `--no-smoothing`
- **results_to_tracklist():** Applies corrections and builds a `Tracklist`
- **Progress persistence:** `save_progress()` / `load_progress()` JSON files enable resuming
- `DEFAULT_DELAY_SECONDS = 15` (between API calls). Field evidence says this is already at the
  conservative end — comparable clients ship 10s — so there is little headroom to go faster.
- **Call log:** each sample is timed, its HTTP attempts drained from a `CallRecorder`, and one
  JSONL line written. Off unless `call_log=` is passed; the CLI resolves it (`_resolve_call_log`),
  so calling `process_single_file()` directly logs nothing. See `call_log.py`.

### `setlist_maker/boundary.py` - Adaptive sampling engine (pure)

- **What it is:** the default `identify` strategy. Sample sparsely, notice where two probes
  disagree, spend the rest of the budget narrowing exactly those disagreements. Measured against
  the synthetic oracle: ~240 Shazam calls for a 4-hour set against the sequential scan's 480,
  with boundaries inside 5s instead of ±30s. Design spec and all measurements:
  `docs/superpowers/specs/2026-08-27-adaptive-boundary-detection-design.md`.
- **Pure — no I/O, no clock, no network, no async.** It consumes completed `Probe`s and answers
  two questions: `next_probe()` ("what should be probed next?") and `segments()` ("what does the
  evidence say this recording contains?"). That is what lets the whole algorithm be tested
  against a fake oracle with no seams to patch, exactly as `progress.render_panel()` is.
- **All state is a deterministic fold over the probe list.** Nothing else is persisted, so
  **resume is replay** — `adaptive.py` loads the probes and re-adds them. `test_replay_equality`
  guards it. This is also why `add_probe()` computes its events by snapshotting interval statuses
  before and after the insert rather than keeping incremental bookkeeping that could drift out of
  sync with the fold.
- **Interval model:** the timeline is cut at each probe's window *midpoint* plus two virtual
  endpoints. Each interval gets a target width by kind — `stride` (90s) for same-track and edge,
  `precision` (5s) for a boundary, `precision_none` (30s) onto an unidentified stretch — and
  priority is `width / target`. That single ordering is the anytime property: early pops are
  breadth-first coverage of the whole file, later pops tighten the worst boundary, so stopping
  after any prefix leaves the smallest maximum uncertainty that many probes could buy.
  `_target` tests the **edge** case before the `None` case, so a recording that opens on
  unidentifiable audio is still *covered* rather than bisected against a sentinel.
- **`_needs_coverage` outranks every boundary consideration.** An interval wider than `stride`
  is split for coverage no matter what its endpoints claim, because a whole track could be
  hiding in it. Without this the guarantee the spec calls unconditional — never miss a track
  ≥ 2 minutes, "from splitting geometry, not from offset trust" — silently becomes conditional:
  a confirmed prediction retired 190s-wide boundary intervals unprobed and **five whole tracks
  vanished** from a 4-hour set. Prediction buys precision, never permission to skip coverage.
- **Offset prediction.** A probe of track B reports where inside B it matched, so one probe can
  locate a boundary that bisection needs four or five for. Two things make it safe:
  - **`_probe_start_estimate` is not `T - O`.** `Shazam.recognize` goes through `shazamio_core`,
    which fingerprints a **centered 10s excerpt** (`fingerprint_segment`) of whatever window it
    is handed — so the matched audio begins `(window - 10) / 2` after the probe does. Measured on
    a real set: uncorrected, a 30s coverage probe and a 12s refine probe of the same track
    disagree by exactly 9.0s, which *exceeds* `offset_tolerance`, so `_trusted_start` would
    reject every track the verification protocol reaches and the prediction path would be dead
    code that never announced itself. Corrected, the two window sizes agree to 0.1s.
  - **P is a lower bound, not an estimate.** A track the DJ cut into mid-song implies a start
    earlier than the real boundary, so P is only accepted once some probe of B *was heard*
    starting just after it. Both the verification probe's placement and the acceptance test use
    the **excerpt**, not the window: `verify_lead = 0.0` puts the fingerprinted audio at
    `[P, P+10]`, which bounds a missed cut-in at `fingerprint_segment / 2` = 5s — exactly
    `precision`, which is why that default holds rather than needing to be relaxed. A cut-in
    instead answers A, and that probe's evidence becomes the interval's new left edge, pushing P
    outside it so the prediction cannot fire again.
- **`_near_existing` compares excerpts, not window starts** — those differ by 9s between window
  sizes, so a start-time test both blocks probes that would hear new audio (measured: it stalled
  bisection at an 18s interval, leaving a 7.4s error) and permits ones that would hear the same
  ten seconds.
- **`segments()` is callable at any prefix**, which is what makes every stopping rule —
  converged, budget, Ctrl-C — one code path. The **phantom rule** replaces the sequential path's
  A-B-A smoothing: a single-probe track is dropped only when it is *pinned* by contradicting
  evidence on both sides into under `phantom_min` **and** its own sample confidence is below
  `singleton_confidence_keep` (the sample's, not its cluster's — the #7 lesson). A lone claim
  with an open flank might genuinely span it, so the rule refuses to drop it until refinement
  squeezes it. A gap between two runs of **one** identity is judged at `precision_none` (30s)
  rather than `phantom_min`, because that is where `_target` stops refining a None-adjacent
  interval: a dropout retiring in (20, 30] could never be narrowed by any later probe, and the
  real set reported one track (The J.B.'s) as three rows around a permanent 22.5s hole. This
  restores the parity with `_smooth_sequence`'s unconditional `A None A` that the spec claimed
  and the first implementation did not have.
- **Contradiction collapse (`_collapsed`).** Probing hardest at a transition means the fold now
  *sees* Shazam changing its mind there, and reported every flip as a track: measured on a real
  4-hour set, Boz Scaggs → Grant Green folded into four segments over 36s alternating with Us3
  (who sample Blue Note, which Grant Green *is*), and a 16s Notorious B.I.G. sat in the cut onto
  Herb Alpert's *Rise*, which it samples. Three gates, none of them removable:
  - **Pinned and measured.** Both neighbours identified — `None` is the absence of an answer,
    not a competing claim, which is what spares the real set's two 12.7s runs beside
    unidentified stretches — and both boundaries pinned within
    `min(target, fingerprint_segment / 2)`. The floor is *not* the plain target: `--precision`
    buys fewer probes, and borrowing its value would let relaxing it silently buy deletions.
  - **Too small to be a track:** under `phantom_min`, or under `stride` inside an alternation
    zone. `_alternation_zones` implements the spec's own never-built rule (*"A, then B, then A
    again"*): a **return** is one identity's consecutive pair separated by an excursion under
    the stride — the span the coverage guarantee says cannot hide a track — and a **zone** needs
    two *overlapping* returns of different identities, i.e. A-B-A-B. One return is A-B-A, which
    stays with `singleton_confidence_keep`. Returns are collected in run order and merged by
    index overlap, and the winner is `max(probes, extent, -first run)`, so the fold stays the
    deterministic replay `test_replay_equality` requires.
  - **Convicted by its own offsets (`_misattributed`).** This is the gate the rule cannot ship
    without. Geometry alone convicts all three real phantoms *and* deletes genuine short tracks:
    measured against the oracle, 100% of real 12–18s tracks and ~60% of real 20s ones, every one
    of which the code before this shipped correctly. Two readings, both measured: **incoherent**
    (the run's own probes disagree about the start by more than the run is long — the second Us3
    run spreads 49.0s across 5.6s of audio; the bound is the run's own extent because 10 of the
    file's 51 multi-probe runs legitimately spread past 4s, one by 455s) and **collide** (the
    run's implied start sits within `offset_tolerance` of a *better-supported* neighbouring
    run's — *Hypnotize* and *Rise* both imply 3973.3, to 0.1s). Support is what makes collide
    one-directional: the phantom collides with the real track exactly as much as the reverse.
    A run with no offsets is never collapsed; the rule needs evidence, and absence of it is not.
    Measured on the oracle's rewind geometry (a DJ pulling a record back, a genuine A-B-A-B):
    the veto acquits 40 of 40, where geometry alone deletes a real 162s record.
  The zone's own best-supported identity is exempt whatever its extent — on the real file the
  *correct* track clears `phantom_min` by 2.8s, less than the engine's own p90 boundary error,
  so it needs protecting by name rather than by arithmetic. Drops are appended to `segments()`'s
  second return value as `contradiction_collapsed` beside `phantom_dropped`, so `adaptive.py`
  writes them to `<base>_events.jsonl` with no wiring. The rule is deliberately **not** monotone
  in track *presence*: it is refinement that licenses it, so a phantom can appear at one prefix
  and be collapsed at a later one. Measured across all 317 real prefixes, exactly two titles do
  that and both are the phantoms; nothing flickers.
- **The oracle grew a `misid_rate` for this.** Every answer `tests/boundary_oracle.py` gave
  named a track actually present in the excerpt, so no seed could produce a misidentification
  and the property gates were green on `_collapsed` vacuously (measured: 80 extra seeds fire
  zero collapses). `misid_rate` gives each track a stable impostor whose offset describes the
  **real** track's position — that being the audio actually fingerprinted — which is what makes
  the two implied starts collide and so what the veto reads.
  `test_misidentified_phantoms_collapse_without_taking_a_real_track` counts "real tracks found"
  from the *probe results*, not from the oracle: at a 12% rate a track thin enough to get one
  probe sometimes has that one probe stolen, and the fold cannot report what it was never told.
  Its efficacy bound is deliberately loose (43 of 89 impostors survive, correctly — a
  per-probe contaminant mostly lands alone inside a long track, where the flanks stay coarse and
  A-B-A belongs to `singleton_confidence_keep`); efficacy is pinned by `tests/fixtures/*.json`.
- Identity clustering is `identify.py`'s `_assign_cluster` / `_normalized_key` reused as-is, so
  the same track under two labels never manufactures a boundary. Note the corollary for
  fixtures: names like `Artist 1` / `Artist 11` score 0.94 and legitimately merge — see
  `tests/boundary_oracle.py`'s `assert_identities_distinct`.
- Tunables live in `EngineConfig`; `--precision`, `--stride` and `--refine-window` expose the
  interesting ones.

### `setlist_maker/adaptive.py` - Adaptive driver (impure)

- **Owns everything `boundary.py` refuses to touch:** the Shazam client, the inter-call delay,
  persistence, signals, the panel and the call log. `process_single_file_adaptive()` is a
  drop-in sibling of `process_single_file()` — same inputs, same output files, different
  sampling strategy — and finishes through the shared `finalize_outputs()` so the two cannot
  produce different artifacts.
- **Progress v2** is `{"version": 2, "audio_duration": D, "probes": [...]}`, written after every
  probe. A **legacy** sequential list is detected by shape and converted in memory to 30s
  coverage probes: that format already carried timestamps (only its *resume* was positional), so
  a half-finished sequential run resumes as an adaptive one with a dense probed prefix, with no
  migration step. The reverse is refused loudly in `identify.py`.
- **`EventLog`** appends one JSONL line per event beside the progress file — the phase-2
  visualizer's input. Append mode so a resume extends history, flushed per event so a `tail -f`
  sees probes land.
- **Anytime stopping.** First Ctrl-C sets a flag and finalizes after the in-flight probe; a
  second aborts (per-probe persistence means nothing is lost either way). `--budget` stops on
  wall clock. Both funnel into the same `segments()` → `results_to_tracklist(deduplicate=False)`
  → `finalize_outputs()` tail as natural convergence, so there is no "interrupted" output shape.
- **The call log is wired in here too**, not only in the sequential driver: adaptive is the
  default, so a log reachable only through `--sequential` would be empty for exactly the
  ordinary runs a throttling question gets asked about. `total` is the engine's live estimate,
  there being no fixed count.

### `setlist_maker/summary.py` - Playlist summary generation
- **generate_summary():** Shells out to the Claude CLI (`claude -p --strict-mcp-config`) from a
  throwaway temp dir to produce a one-paragraph set description. Best-effort: returns `None`
  (warns and continues) if the CLI is missing, errors, times out, or returns nothing. On by
  default; suppressed by `identify --no-summary`. The result is stored on `Tracklist.summary`,
  rendered by `to_markdown()` inside the `<!-- summary -->` fence, and recovered from it by
  `parse_markdown_tracklist()` for editor round-trips. The markdown is the summary's **only**
  persistent store — `to_json()` deliberately omits it — so a parse that loses it loses it for good.

### `setlist_maker/chapters.py` + `setlist_maker/artwork.py` - Chapter markers & artwork
- **embed_chapters():** Writes ID3v2 CHAP/CTOC frames into an MP3 for podcast players.
  Saves as **ID3v2.3**, never mutagen's default v2.4 — players misparse v2.4 syncsafe
  CHAP sub-frame sizes once an artwork APIC sub-frame exceeds 128 bytes and silently
  drop every chapter (#17). Guarded by an ffprobe round-trip regression test.
  After `save()`, `_order_chap_frames_chronologically()` rewrites the tag so the CHAP
  frames sit in time order: mutagen sorts every frame by serialized size at save time
  (only APIC keeps insertion order), so differing chapter artwork scatters them, and
  while conforming players follow `CTOC`, ffprobe/ffmpeg — and hosts and web players
  built on them — enumerate CHAP frames in file order and show a shuffled list (#33).
  mutagen has no ordering hook and all its serialization is private, so this permutes
  frames in the saved bytes instead: the tag is a fixed-size region, so the audio never
  moves, and any layout it doesn't expect (other version, tag/frame flags, short frame)
  leaves the file exactly as mutagen wrote it. Guarded by `TestChapterFrameOrder`, whose
  artwork *shrinks* per chapter so an unfixed embed writes them in exact reverse.
- **embed_chapters_for_tracklist() / _episode_cover_image():** The episode cover comes from the
  track starred in the web editor (`Track.is_episode_cover`), falling back to the original rule —
  the first track with *real* artwork — when nothing is starred **or** the starred track's lookup
  turns up nothing. `--cover` still outranks both, being seeded before the derivation runs.
  `source_artwork()` returning `None` must keep meaning *skip*: feeding it to
  `create_chapter_image()` yields a gradient card whose truthy bytes permanently block every
  later track from supplying a real cover, a regression that already happened once.
- **fetch_artwork():** Waterfall lookup across Shazam CDN, iTunes, Deezer, MusicBrainz/Cover Art
  Archive. Short-circuits on the first source that yields bytes.
- **artwork_candidates():** The picker's counterpart — asks *every* source and returns what each
  offers, deduplicated by URL in waterfall order (#20). Each source's single-result helper
  (`search_*_artwork`) is now a thin wrapper over its `*_artwork_candidates` sibling called with
  `limit=1`, so the two paths cannot drift. Only MusicBrainz costs more per candidate: Cover Art
  Archive reveals whether a release has a front cover only by being asked, one request each.
  `deezer_artwork_candidates()` retries with a **plain term query** when Deezer's advanced
  `artist:"..." track:"..."` syntax comes back empty, which it does for a large share of real
  tracks (measured: 0 rows for Daft Punk / One More Time and Kraftwerk / Autobahn, against 48 and
  164 plain) — so Deezer had been contributing nothing to the waterfall either. The retry fires
  only when Deezer *answered* with no rows: `_deezer_search()` returns `None` for a failed request
  and `[]` for an empty one, so an unreachable Deezer still costs one timeout, not two. Existing cached
  composites are unaffected (the key doesn't change); only fresh lookups can now resolve to Deezer,
  and tracks already marked artless keep their sticky `.fallback` marker until the cache is cleared.
- **download_image():** Refuses anything but `http(s)` via `is_fetchable_url()`. `urlopen`'s default
  opener handles `file:`, `ftp:` and `data:`, and since the picker lets a user *type* a cover URL
  that is persisted to the sidecar and re-fetched by this process on every later run, an unfiltered
  one would be a local-file read primitive reachable from a page (#20).
- **create_chapter_image():** Builds per-chapter artwork with an MTV-style lower-third overlay
- **load_cover_image():** Normalizes a user-supplied episode cover (`--cover`) for embedding —
  center-crop to square (`create_chapter_image()` hard-resizes instead, which squashes), then
  the shared `_compress_to_jpeg()`. Deliberately skips the lower-third overlay: hand-picked
  cover art is finished, not a generated chapter card. Raises `CoverImageError`.
  `embed_chapters_for_tracklist()` seeds `episode_image` with it, which both uses it and
  short-circuits the first-track derivation (already guarded on "still `None`"). Independent
  of `fetch_art`, so `--cover --no-artwork` embeds just the cover.

### `setlist_maker/artwork_cache.py` - Chapter image cache
- **chapter_image():** The single path from a track to its chapter composite —
  `source_artwork()` + `create_chapter_image()`, cached on disk. Called by both the
  web editor's `/api/artwork` preview and `embed_chapters_for_tracklist()`, so the
  image previewed is byte-identical to the one embedded (#20).
- **source_artwork():** The one place a track's artwork is resolved, caching *both*
  answers — the fetched bytes in `<key>.src`, or an empty `<key>.fallback` marker when
  the lookup came back empty. The marker is a **negative-result cache**: it stops a
  track with nothing findable from re-running the six-request waterfall every run, and
  being a file it survives across processes, so a later `chapters` run (a fresh process,
  cache hits only) still knows. Caching the source separately from the composite is what
  lets the episode cover reuse a track's fetched art under different overlay text with
  no second lookup. A `None` return means "no artwork for this track" and is the
  caller's to interpret — `embed_chapters_for_tracklist()` uses it to pick the episode
  cover from the first track with *real* art rather than the first identified one.
  Cached bytes win over a marker if both somehow exist.
- **Cache key is a content hash** of (artist, title, coverart_url, size), so an edit
  regenerates structurally — there is no invalidation code path. Lives in
  `$XDG_CACHE_HOME/setlist-maker/artwork` (else `~/.cache/...`). Every loader that
  feeds the editor or `chapters` must supply `coverart_url` (the JSON sidecar's, via
  `_load_tracklist_with_artwork_urls()`) — parsing markdown alone leaves it `None` and
  silently keys the preview differently from the embed.
  Regeneration is necessary but **not sufficient** on a correction: `fetch_artwork()`
  tries `coverart_url` ahead of every search, so a corrected track whose stale Shazam
  URL survived would re-key, re-fetch, and composite the *same wrong cover* under the
  new text. `editor.apply_track_edit()` clearing the URL is what makes the new key
  resolve to new art (#30).
- **artwork_options():** The picker's cached fan-out. Keyed with `coverart_url` **omitted** —
  which alternates exist depends on artist/title/size, while a saved URL is one particular
  *answer*, not an input — so the `.cands` entry sits beside the no-URL variant's files.
  Unlike `source_artwork()`, an empty result is **not** cached: "no alternates" far more often
  means the network was down than that the track has none, and this is an interactive surface
  where retrying costs one click rather than a whole re-run.
- Per-key locks — `RLock`, since `chapter_image()` calls `source_artwork()` for the
  *same* key while still holding its own lock — dedupe concurrent requests; a
  semaphore caps simultaneous network fetches at 4 (compositing is local PIL work and
  is not capped). An unwritable cache degrades to in-memory generation rather than
  failing.

### `setlist_maker/editor.py` - Interactive TUI editor
- **TracklistEditor:** Textual app providing spreadsheet-like interface
- **EditTrackScreen:** Modal dialog for editing artist/title fields
- **CorrectionsDB:** Persistent storage for user corrections, at
  `$XDG_CONFIG_HOME/setlist-maker/corrections.json` (else `~/.config/...`), via
  `fileio.config_dir()` like `episode.py`'s config, so the tests' `isolated_user_config` covers it.
  It is every correction ever learned, so a load never risks it (#64). An unparseable or
  wrong-shaped file is renamed `corrections.json.corrupt-<time>` with a warning, and the session
  starts empty. It used to load as `{}`, and the next save then overwrote the file with only that
  session's corrections. A file that exists but can't be *read* can't be moved either, so `save()`
  refuses for the rest of the session. Saves are atomic.
- **apply_track_edit():** The one place a correction lands on a `Track` — shared with the
  web editor (like `save_tracklist()`) so the two front ends cannot drift. Stamps
  `original_*` once, records the correction, and **clears `coverart_url`**: that URL is
  evidence attached to the *original* identification, and leaving it attached pins the
  chapter art to the misidentified track (#30). No-ops when nothing changed, so
  rejecting a row — which re-sends its unchanged fields — keeps art that is still right.
  The one exception is `Track.artwork_pinned`, set when the user picked a cover in the web
  editor's picker: that URL is not Shazam's guess about a stale identification but the user's
  answer about *this* track, so clearing it on a later typo fix would silently discard it (#20).
  Both fields are **stripped** first (the web editor already did so at its own door), which is
  what lets the markdown decide a row's shape from the value alone; stripping before the no-op
  comparison also keeps a row re-sent with stray space from counting as a correction (#44).
- **parse_markdown_tracklist():** Parses existing markdown files for editing. The set
  description is read from the `<!-- summary -->` … `<!-- /summary -->` fence `to_markdown()`
  writes, and the lines it occupies are then **excluded from every other scan** — header, date
  and tracks. Both halves matter: without the fence a description shaped like `1. **X** - Y
  (0:00)` ended the summary early, and without the exclusion that same line was *also* read as
  a real track, so reopening a set silently traded the description for a phantom track (#16).
  HTML comments because the `.md` is a published artifact and they render as nothing; a
  description line that reads exactly like a marker is escaped with a backslash on write and
  unescaped on read (an involution, so `\<!-- /summary -->` survives too). A file with **no**
  fence — anything written before this existed — falls back to the old heuristic (prose after
  `*Generated on*`, ending at the first blank or track-shaped line), which is why old
  tracklists still reopen; the next save rewrites them fenced. Nothing can disambiguate a
  legacy file, and nothing else can recover from it: the summary is not in the sidecar.
  Two hand-edit shapes the fence can't fix are **warned about** rather than fixed silently,
  since a fix for silent loss shouldn't add a quiet failure of its own: an opening marker
  with no closing one (falls back to the legacy scan rather than reading to EOF and returning
  a tracklist with no tracks), and a closing marker moved *below* the listing, which swallows
  every track — loud because the next save would write that reading back as a well-formed
  file and take the sidecar's artwork with it. Two write-side details are also load-bearing:
  `to_markdown()` normalizes carriage returns (the reader opens in text mode, where a lone CR
  *is* a line break, so an un-normalized one splits a line the writer never escaped and can
  close the fence from inside), and it leaves a blank line inside each marker so renderers
  that escape raw HTML instead of honoring it don't fold the markers into the description's
  paragraph.
- **The listing's four shapes (`TRACK_LINE_PATTERN` / `to_markdown()`):** a track may know its
  artist, its title, both, or neither, and the markdown carries all four — `**Artist** - Title`,
  `*Unknown artist* - Title`, `**Artist**`, `*Unidentified*`. Totality is the point, not tidiness:
  the `.md` is **authoritative for which tracks exist** (the sidecar is only joined onto it by
  timestamp), so a shape the writer can emit but the pattern can't match is a row *deleted* on the
  next read, which is what `**** - Title` did to every artist-less track — reachable in one move,
  since the web editor's "＋ Add below" inserts a blank row and nothing between there and the file
  objects to a title with no artist (#44). The empty side gets a **marker rather than being left
  out** because `1. Titled Only (3:00)` is indistinguishable from prose, and this parser already
  learned that lesson in #16; the markers are italic and real values are always **bold**-wrapped,
  so no user-typed value can serialize to one — an artist literally named `*Unknown artist*` is
  written `***Unknown artist***` and reads back as itself. The artist span is `(.*?)`, not `(.+?)`,
  so files already damaged by the old writer give their track back rather than staying short — but
  the **title** span must stay `(.+?)`: the line is matched unanchored, so a title span that can
  match nothing lets the time group bind to a `(m:ss)` *inside* the title, and `**A** - (1:23)
  Reprise (10:00)` reads back as an empty title at 1:23 — losing the title, missing the sidecar
  entry (joined by timestamp) and moving the chapter mark, then cementing all of it on the next
  save. The legacy `**Artist** -  ` shape needs no help from a nullable span anyway, since `\s*-\s*`
  is greedy and the caller strips what it leaves. Anchoring the pattern with `\s*$` would fix the
  related #45 (a `(m:ss)` mid-title) but is **not** a free win: it stops matching a hand-annotated
  line like `1. **A** - T (0:00) [live]`, which turns an annotation into a deleted row — this
  format's one unforgivable failure. Because the pattern also decides where a *legacy* description
  ends, every branch is anchored on a marker or `**`, never on bare text. Both `to_markdown()` and `is_unidentified` work on
  **stripped** values, so a whitespace-only field is stored as the empty field it already is —
  untreated it survived one round trip (the reader strips) and was deleted by the next, putting
  the loss a save away from the edit that caused it.
- **Audio preview:** `p` previews the selected track's 30s window via `PlaybackController`
  (see `playback.py`). `_resolve_audio_path()` locates the source audio (threaded in from the
  CLI when known, else discovered beside the markdown). Playback stops on navigation/reject/edit
  and on unmount; gated by `playback_enabled` (set once in `on_mount`). The 0.5 s
  `_tick_playback` readout poll returns early when `is_running` is false: Textual clears
  that flag, unmounts the screens, and only then stops the app's timers, so a tick in that
  window would otherwise `NoMatches` on the label during shutdown (#35).
- **Pilot tests** (`tests/test_editor_edit.py`, `test_editor_playback.py`) drive the app via
  `app.run_test()` inside `asyncio.run()` (no pytest-asyncio). A step that depends on a
  multi-hop message chain — e.g. Enter → `Input.Submitted` → `focus()` → deferred
  `set_focus` — must wait on that state with the shared `wait_until` fixture
  (`tests/conftest.py`), not assume `pilot.pause()` let it settle: `pause()` only waits for
  messages already queued plus a CPU-time idle heuristic that a *starved* process also
  satisfies, which is why the roundtrip test flaked only under load (#35).

### `setlist_maker/web_editor.py` - Browser tracklist editor
- **run_web_editor():** Drop-in sibling of `editor.run_editor()` that serves a
  single-page editor (`web_editor.html` + `web/`) from a loopback `ThreadingHTTPServer`
  on an ephemeral port and opens the browser. Reuses `Track`/`Tracklist`,
  `CorrectionsDB`, and the shared `save_tracklist()` / `resolve_audio_path()`
  helpers from `editor.py` so the TUI and web front ends never drift.
- **Endpoints:** `GET /` (page), `GET /api/tracklist`, `POST /api/save`
  (writes `.md` + `.json` + corrections via `save_tracklist`), `GET /api/audio`
  (HTTP Range streaming powering the in-browser scrubber), `GET /api/artwork?index=N`
  (the JPEG chapter composite for one track, from `artwork_cache.chapter_image()` —
  the exact bytes `chapters` will embed; served `Cache-Control: no-store` since the
  page re-requests after a save. 404s on an unparseable or out-of-range `index`, and
  on an unidentified track, which `chapters` skips too. Keyed by **saved** track
  state, not the page's live fields, so the preview always reflects what would be
  embedded), `GET /api/artwork/source?index=N` (the cover as found, with no overlay:
  `source_artwork()`'s bytes, resolved through the same `_track_for_query()`, plus a 404 when
  no source has art -- see **One picture per track** below), `GET /api/artwork/options?index=N` (the picker's candidate list — see
  **Artwork curation** below), `POST /api/done` (graceful shutdown → returns control
  to the CLI), `GET /api/probes` + `GET /timeline.js` (the timeline -- see below), `GET /api/peaks` (the
  waveform, see `peaks.py`),
  `POST /api/sample` (click-to-sample, see `sampler.py`), and the upload / episode cover /
  embed endpoints described under `uploads.py`. Both artwork endpoints resolve
  `index` through the shared `_track_for_query()`, so they answer for exactly the same set of tracks and 404 alike.
- **Host-header guard:** `_reject_foreign_host()` runs at the top of both `do_GET`
  and `do_POST` — a single gate, so a new endpoint cannot forget it. Requires the
  loopback name (`127.0.0.1`/`localhost`, case-insensitive) **and** this server's
  exact ephemeral port; anything else gets `403`. Binding loopback only stops other
  *machines*, not a page the user is already viewing: a hostile site can point its
  own name at 127.0.0.1 (DNS rebinding), after which the browser treats this server
  as same-origin and lets the page read `/api/tracklist` and `/api/audio` (the source
  recording) and POST to `/api/save`, whose corrections apply to every future run.
  A rebound request still carries the attacker's `Host`, which is what this rejects (#26).
- **Pure helpers:** `tracklist_to_api()` and `apply_edits()` are socket-free and
  unit-tested directly. `apply_edits()` maps edits onto existing tracks by stable
  `index` and puts each one through `editor.apply_track_edit()` — the TUI's own
  correction step; an edit with **no** `index` is a track inserted via the page's per-row
  "＋ Add below" control — it's appended with its own `timestamp` and the list is
  re-sorted into chronological position (inserts aren't corrections, so none is
  recorded). An indexed edit's `timestamp` moves that track's start (#55): not a correction,
  and a value that isn't a finite number of seconds >= 0 leaves the start alone rather than
  defaulting to 0 as an insert does. Any moved start re-sorts the list, which bumps the
  revision, and the save response carries `revision`. The page sends existing rows by `index`
  (not array position, which inserts shift) and reloads after a save that added rows or changed
  the revision, so a re-save can't duplicate or 409.
  Opened with `identify --web-edit` / `-w`; mutually exclusive with `--edit`.
- **Saving is serialized and revisioned (#64).** `ctx.state_lock` is held by `/api/save` and
  around the embed in `/api/chapters`, and by `/api/tracklist` while it snapshots the tracks.
  The lock alone does not make a double-submitted save safe. The second copy would insert its
  new row again, and once the first copy's insert has re-sorted the list, its stale indices
  rename other tracks. So `ctx.revision` is bumped whenever a save changes which index names
  which track (and when `WatchSession.finish()` swaps the tracklist in). `/api/tracklist` returns
  it, the page sends it back, and a save carrying an older revision is a 409 that asks the user
  to reload. A payload with no `revision` (an older page) is not checked. The page's `save()`
  also joins a save already in flight instead of sending a second one. A payload that fails
  validation (a `ValueError`, including a body that isn't JSON) is a 400, not a 500.
- **Editable set description:** the page renders `Tracklist.summary` as a `<textarea>` in the
  inspector's Set tab (empty shows an "add a description" placeholder). Its
  text rides along in the `POST /api/save` body; `_handle_save()` forwards it as
  `apply_edits(..., summary=...)`, which uses an `_UNSET` sentinel — an absent
  `summary` key leaves `tracklist.summary` untouched (so older clients can't wipe it),
  while a sent value is whitespace-normalized to one paragraph (blank/None clears it).
  That normalization used to be what kept the `to_markdown()` ↔ `parse_markdown_tracklist()`
  round-trip lossless; since #16 fenced the description the round-trip is lossless for *any*
  text, so it is now only house style — whatever the user types, including a numbered line
  that looks exactly like a track, comes back verbatim.
- **Artwork curation (#20, #61):** clicking a row's thumbnail opens `#art-dialog` (see
  **Dialogs** under `setlist_maker/web/`): a header with the track and ‹ › (⌘[ / ⌘] step to the
  previous / next track with art, skipping rejected ones, without closing, and the selection
  follows), the composite with the **☆ Episode cover** toggle on the left, and the candidate grid,
  a paste-a-URL box, upload and **Automatic** on the right. Only the grid scrolls. Candidates
  load when the dialog opens (they used to wait for a *Choose artwork…* click); the server caches
  each track's answer. The arrows walk the tiles and Enter is the tile's own click. Stepping
  re-renders the grid, which drops a focused tile, and at either end of the set the ‹/› just
  pressed turns disabled, which drops focus too; `stepArtwork()` / `keepFocus()` put it back on
  an enabled button or the tile in use, since a dialog without focus never sees its own keys. A
  step waits 250ms before looking up its covers, and leaving a track aborts its lookup: the
  browser allows six connections to this server, and lookups queued behind the network held them
  all, so a Save sat waiting (measured: >15s after walking the set). `_send_json` treats the
  aborted client's broken pipe as ordinary. The ★ toggle is
  disabled for a rejected track, which the sidecar does
  not carry (the server refuses the star for one too, so a stale client cannot clear a valid choice
  and store nothing in its place); and `[hidden] { display:none !important; }` is required because
  author `display` rules otherwise beat the UA stylesheet's `[hidden]` rule.
  It lives in the static markup **outside `#list`**, which Preact owns, and it holds the open
  track as an **object reference** (`artTrack`) for the same reason `playingTrack` exists — a
  re-sort or insert moves array positions underneath it.
  Tiles load candidate URLs **directly from the source**: the
  covers differ only in the picture, and compositing each one server-side to show the identical
  lower-third would add latency and nothing else. Which tile reads as in use comes from the
  server's own `current` flag (`artCurrentUrl`), *not* from comparing `artTrack.coverart_url`:
  the endpoint offers the in-use cover through `resize_cover_art_url()` at the chapter size, so
  a Shazam URL saved at 400px arrives as the 600px one and a raw string compare marks nothing.
  Live page state outranks saved state in three places, all for the same reason -- the panel is
  the surface where the two most often differ: `showArtwork()` restores an unsaved pick instead of
  requesting the (pre-pick) server composite; `loadAlternates()` adopts the server's `current`
  flag only when the track has no pick; and the *search* is run on the artist/title the page
  passes, since correcting a misidentification and fixing its cover is one workflow and the stale
  name would offer covers for the song being corrected away.
  A pick sets `coverart_url` (the artwork cache's key, so it regenerates on its own) plus a
  client-only `_art` flag, which is both why the save payload includes the URL at all — sending
  it for every row would re-pin art `apply_track_edit()` means to drop — and why every
  thumbnail previews the raw image until the save (see `artFor()` below). A pasted URL is loaded into an
  `Image` first: the server falls *through* its waterfall on a URL that will not download, so a
  dead link would not error, it would quietly composite another source's art under this track.
- **One picture per track (#60).** Every thumbnail -- the list row, the timeline block, the
  inspector and the player bar -- takes its background from `artwork.artFor(t, i)`. They used to
  disagree: the list showed the server composite and the rest drew `coverart_url`, which a
  track whose art came from further down the waterfall doesn't have (10 of 64 on the 09-28 set),
  so it had art in the list and a gradient everywhere else. `artFor()` is two CSS layers, the
  source cover (`/api/artwork/source?index=N&v=artVersion`) over the track's gradient: still
  loading, or a 404 for no art anywhere, paints nothing and the gradient shows through, so the
  page keeps no load state. An unsaved pick (`_art`) shows the raw choice instead. The composite,
  with its lower-third, is shown only in the artwork panel, which previews what Embed writes.
  `S.artVersion` is bumped by every save **and** every `load()`, since after a reload an index
  can name another track. Measured: one request per track across all four surfaces despite
  `no-store`, and panning the timeline asks only for newly revealed thumbs.
- **Theming:** one set of selectors, two palettes. Every color on the page reads a
  `:root` custom property; `@media (prefers-color-scheme: dark)` redefines only the
  tokens, and `color-scheme: light dark` (on `:root` and as a `<meta>`) lets native
  controls — seek slider, buttons, textarea, scrollbars — follow the OS. The player bar
  and toast share `--player-*`: dark chrome on the light page, an elevated hairlined
  surface on the dark one so they still read as bars. The only scheme-free literals
  are the dialogs' `::backdrop`, the thumb placeholder gradients (`GRADS`), and
  white text on the accent button / those gradients. Light mode is pixel-identical
  to the pre-theming page (#34).
- **Track-focused player:** the scrubber spans the *current track's window*
  (its timestamp → the next track's; the last → audio duration), not the whole
  recording. On a 4-hour set the global bar was 13.6 s/px, so fine positioning
  was unavailable; a window brings that down to roughly 0.1-0.2 s/px depending
  on track length (measured ~0.143 s/px on a 3-minute track). Windows come from
  `windowFor(i)`, derived on demand so an inserted row needs no bookkeeping. The
  playing track is held only as an object (`S.playingTrack`); its position is
  `model.playingIndex()`, derived each time, so a splice or re-sort cannot leave a
  stale index. `load()` replaces every object, so it re-resolves the playing track
  by position, since a reload keeps the order. Windows
  are clamped to ≥1s so duplicate timestamps cannot divide by zero, and
  `trackIndexAt()` lets the playing track win while the position is still inside
  its own window — its backwards walk otherwise resolves a shared timestamp to
  the *later* track, which re-scoped (and scrolled) away from the earlier one on
  the first `timeupdate` tick, so it could never be selected (#37). Playback
  deliberately runs *past* a window's end and re-scopes via `trackIndexAt()` —
  hearing the transition is how a boundary gets verified — and ±15s seeks clamp
  only to `[0, duration]`, never to the window, for the same reason. The playing
  row is highlighted (`.row.playing`) and scrolled into view whenever
  `setPlayingIndex()` is called with `scroll: true` — automatic boundary
  advance and manual prev/next, *including* `prevTrack()`'s restart branch,
  which scrolls even though the index is unchanged — and left unscrolled only
  for a row's own ▶ button, since that row is already under the cursor.
  `render()` also calls `syncPlayerBar()` so the player bar's label, count and
  artwork stay correct after an edit or insert.
  Keyboard: see **The keyboard map (#58)** under `setlist_maker/web/`. Prev/next is ⌥↑/⌥↓
  there, since ↑/↓ move the selection.

### `setlist_maker/peaks.py` - Waveform peaks (#57)

- `GET /api/peaks` serves int8 (min, max) pairs at `PEAK_RATE` (50/s) behind a small header
  (`"SMPK"`, version, rate, count, duration). That is 1.4MB for four hours, and finer than the
  timeline's deepest zoom.
- **Never `load_audio()`.** ffmpeg decodes to 8kHz mono s16 on a pipe and `audioop.minmax`
  reduces each bin in C (audioop-lts on 3.13+, already a dependency; the 3.11-3.12 deprecation
  warning is silenced at import). Decoding the MP3 is the slow part, ~9-10s for four hours on
  one thread, so the file is cut into `SEGMENTS` (8) stretches of whole seconds decoded in
  parallel: ~4s. Each stretch is padded or trimmed to exactly its bins so a stretch that decodes
  a few samples long cannot shift the ones after it. Measured on the real CBR set against a
  single pass: 19 bytes of 1.4M differ, all at the seams.
- Cached in `$XDG_CACHE_HOME/setlist-maker/peaks`, keyed on the resolved path, mtime and size,
  with one lock per key so a reload during the first computation waits rather than starting a
  second fan-out. Embed rewrites the MP3's tag, so the first request after an Embed recomputes.
- The page fetches it once, after the tracklist, and draws without it meanwhile ("reading the
  waveform…" in the toolbar's activity area). A set with no audio gets a 404 and simply has no
  waveform.

### `setlist_maker/fileio.py` - Atomic writes and the config directory

- **`atomic_write_bytes` / `atomic_write_text`:** a temp file in the same directory, fsynced,
  then `os.replace`d over the target, so a crash or a full disk leaves the previous file. Used
  for the `.md` and `.json` (`save_tracklist()`, which the identify run's own output also goes
  through), the corrections DB, the sequential progress file, uploads, and the episode and
  config files. The replaced file keeps its mode, since `mkstemp` would make it owner-only, and a
  symlinked target stays a symlink. The umask is read once at import: reading it means briefly
  setting it to 0, process-wide, and the web editor is threaded.
- `save_tracklist()` writes the **sidecar before the `.md`**. If only one lands, a new sidecar
  under an old `.md` is harmless, but a new `.md` over an old sidecar re-joins a corrected track to
  the stale Shazam URL (#30).
- **`config_dir()`:** `$XDG_CONFIG_HOME/setlist-maker`, else `~/.config/setlist-maker`. Leaf
  module, standard library only.

### `setlist_maker/uploads.py` - Uploaded artwork and episode covers

- **Files beside the tracklist, not in the cache**, so they travel with the set (iCloud
  included) and survive a cache clear: track art in `<set>_artwork/<hash>.jpg`, the episode cover
  as `<set>_cover.jpg`. `<set>` is `progress_path_for`'s stem rule.
- **Track art is an `upload:<hash>.jpg` reference in `coverart_url`**, not a URL:
  `download_image()` (http(s) only) is never asked to fetch it. `_picked_artwork_url()` accepts
  it beside http(s), and the pick pins it like any other. The name is a hash of the *stored*
  bytes, so it is also the artwork cache's key, and every read validates it against `_REF`
  before joining it onto a path -- the sidecar is hand-editable.
- **Resolved in `artwork_cache.source_artwork(..., uploads_dir)`**, which reads the file and
  touches neither cache. A missing file (an iCloud folder not synced yet) must not write a
  `.fallback` marker or cache a gradient composite, or the wrong art would outlive the sync;
  `chapter_image()` skips caching exactly that case. Every chapter path has to thread
  `uploads_dir` (`embed_chapters_for_tracklist(tracklist_path=)`, the editor's `/api/artwork`),
  or an upload silently becomes the gradient card.
- `store_upload()` decodes with Pillow, centre-crops square (`create_chapter_image` resizes, so
  a portrait would be squashed), caps at `MAX_STORED_EDGE` and re-encodes JPEG. HEIC fails with
  a message saying so rather than adding a plugin dependency.
- **The episode cover is a file because the sidecar is a bare list** (see *The JSON sidecar's
  shape*) and the cover belongs to the set. Precedence in `embed_chapters_for_tracklist`:
  `--cover` > `<set>_cover.jpg` > the starred track > the first track with real art; the file is
  honoured with `--no-artwork`, as `--cover` is. Uploading one un-stars every track (page and
  server), and starring a track removes it (page), so there is one choice.
- **Endpoints.** `POST /api/upload` takes the raw file with its own `image/*` type and refuses
  anything else -- `multipart/form-data` and `text/plain` are types a cross-site form can POST
  to a loopback port without a preflight, and this endpoint writes files. An oversized body is
  drained in chunks before the 413, or the client sees a reset instead of the answer. Storing
  is not choosing: the page pins the reference with an ordinary edit (undoable, saved on Save).
  `GET /api/upload/<hash>.jpg` is `immutable`; `GET /api/cover` is `no-store`. The save
  payload's `cover` key is a reference, `null` (remove) or absent (unchanged), validated before
  anything is applied; so is its `episode` object (see `episode.py`). `POST /api/chapters` runs `embed_chapters_for_tracklist` on the *saved*
  tracklist (the page saves first), one at a time, never while live; the page pauses the
  `<audio>` around it and reloads it after, because mutagen rewrites the file it streams from.
  The endpoint imports `cli` lazily -- `cli` imports this module.

### `setlist_maker/episode.py` - The episode's title and artist

- Written by `embed_chapters(title=, artist=)` as top-level `TIT2` **and** `TALB` (title) and
  `TPE1` (artist), via `setall`, which replaces only top-level frames -- each chapter's title is
  a `TIT2` *sub-frame* of its `CHAP` and is untouched. A blank value writes nothing, leaving
  whatever the recorder tagged. `test_ffprobe_reads_the_tags_and_the_chapters_in_order` guards
  that the extra frames don't disturb `_order_chap_frames_chronologically`.
- Stored per set in `<set>_episode.json` beside the tracklist (a sibling file, like the cover,
  because the sidecar stays a bare list). Missing keys fall back to defaults independently; a
  saved `""` stays blank. Defaults: the title is the set name's leading `YYYY-MM-DD` as
  "September 23, 2026", and the artist is the last one saved from the editor, remembered as
  `episode_artist` in `$XDG_CONFIG_HOME/setlist-maker/config.json` (else `~/.config/...`).
  `tests/conftest.py`'s autouse `isolated_user_config` points that at a temp dir, or a test
  that saves an episode would rewrite the user's real default.
- `embed_chapters_for_tracklist(tracklist_path=)` reads them, so the `chapters` command and
  the editor's Embed agree. The page gets `episode` from `/api/tracklist` and always sends it
  back on Save; the fields are plain text inputs, like the description, so they are compared
  for dirtiness (`textState`) rather than recorded as undo steps, and ⌘Z in them is native.

### `setlist_maker/web/` - The page's modules (#53)

- `web_editor.html` is markup only. The stylesheet is `web/editor.css`, and the script is native ES
  modules with no build step, served from the package at `/web/<name>.js` (a plain name and
  `.js`/`.css` only, so a request can't leave the directory). `/timeline.js` is still a classic
  script loaded first, because Node `require`s the same file in tests.
- Modules: `state` (shared state), `dom` (element lookups, `el()`, `icon()`, `toast()`), `util`, `model`
  (track windows, which track holds a moment), `history` (dirty, undo/redo), `list`, `player`,
  `grid` (the timeline), `inspector` (plus sampling), `issues`, `edits` (merge, split, rename,
  shared by every pane), `artwork` (thumbs, picker, uploads, episode cover, Embed), `dialog`
  (the dialogs' shared behaviour and `ask()`), `commands` (every action, for the menus and
  ⌘K), `menu` (the context menu), `palette` (⌘K), `activity` (what is running, in the
  toolbar), `keys` (the keyboard map) and `main` (load, render, save, Done, the live poll). `main` is the entry and
  imports the rest.
- **State that more than one module reassigns lives on `S` in `state.js`**, because an imported
  binding is read-only. A `let` that only its own module reassigns stays there, exported if
  others read it. The modules import each other in cycles. That is safe only because the
  top-level code in each module just registers listeners: calling into another module at load
  time can hit its `let`s before they're initialized. Anything that must run at startup goes in
  `main.js`, after the imports, or in a `queueMicrotask`.
- **Module scripts run after the document has parsed**, unlike the inline script they replaced.
  So `player.js` also runs its `loadedmetadata` handler when `audio.readyState` is already
  ≥ 1. Without that, a quickly loaded `<audio>` fired the event before anything listened, the
  player stayed disabled, and every playback key did nothing.
- `test_every_module_import_is_exported` is the only check on the imports: with no bundler, a
  bad import fails only in the browser and takes the whole page down.
- **Rendering is Preact** (#53): vendored Preact 10 and htm (tagged templates, so no JSX and no
  build). See `web/LICENSES.md` for versions and hashes; an import map in the page resolves the
  bare `"preact"` the hooks file imports, and the page's own modules go through `ui.js` only.
  The four panes `render()` used to rebuild wholesale are components: the list, the timeline
  grid, the inspector and the queue. Each `renderX()` is a top-level Preact render into its
  container, which is synchronous. `splitAtPlayhead()` relies on that when it focuses the new
  track's field straight after. Rows, blocks, thumbs and seams are keyed by track object
  (`ui.keyOf`), so an edit replaces the nodes that changed (measured: rejecting one row removed
  82 subtrees before, 2 nodes now). The player bar, the episode cover strip and the artwork
  panel are still updated in place by hand.
- Preact 10 traps this page hits:
  - Preact compares `value` against the input's live DOM value on every render, so a
    re-render resets a field mid-keystroke unless the typed text lives in component state. The
    inspector's `NameField` and the list's `EditFields` keep drafts, and that is what replaced
    the old "don't rebuild the inspector while a field has focus" guard. Escape writes the saved
    value into the DOM *before* blurring, or the blur's `change` would commit the draft.
  - Preact diffs against its last vnode, not the DOM. So an attribute that code outside
    Preact also writes must stay out of the vnode: `.tl-playhead` has no `hidden` or `style`
    props, because `updatePlayhead()` moves it on every `timeupdate`. The hover `.tl-cursor` is
    appended by hand to a Preact-owned `.tl-lanes`; Preact leaves trailing nodes it didn't
    create alone.
  - List thumbs ask `artwork.whenVisible()` to tell them when they scroll near the viewport,
    and unsubscribe in their effect's cleanup; until then `artFor(t, i, false)` is just the
    gradient.
- Inline editing is `S.editing` (a track object, plus `S.editField`, the cell that was clicked)
  rather than DOM surgery. **Escape cancels.** From the keyboard (`S.editSelect`) the field's
  text starts selected, and Enter selects the next field's, so typing replaces the name; Enter
  and Escape hand focus back to the row.
  It used to commit: the old Escape rebuilt the list, Chrome fires `focusout` when the focused
  input is removed, and the commit-on-leave handler then saved what had been typed.
- **Dialogs (#61).** Every dialog is a native `<dialog>` opened with `showModal()`, which gives
  the focus trap, Escape, `::backdrop` and an inert page behind it; nothing on the page uses the
  browser's `confirm`/`alert`/`prompt` (a wiring test checks). `dialog.js`'s `sheet(dlg)` adds the
  shared behaviour to a static one: `[data-close]` buttons and a backdrop click (tested by the
  pointer's position, since a click on the dialog's own padding also targets the `<dialog>`)
  close it, and Tab **wraps** at either end, because Chrome otherwise lets Tab leave a modal for
  its own toolbar, where keys never reach the dialog. Clean-up goes in the dialog's `close`
  listener, the one path every way of closing takes. `ask({title, body, buttons})` is the in-app
  confirm, resolving to the button's `value` or `null`. Done uses it for **Save & close /
  Discard / Cancel**, then shows a styled closed screen. Two traps: a key handled inside a dialog
  still bubbles to the document, so one that closes the dialog (the sheet's `?`) must
  `stopPropagation()` or the document handler, finding no dialog open, acts on it too; and the
  top layer covers any `z-index`, so `#toast` is a `popover="manual"` shown afresh for every
  message, which puts it above a dialog opened before it.
- **Icons, save state, activity, toasts (#62).**
  - Icons are `<symbol>`s in one sprite at the top of the page (`#icons`, Lucide's shapes, ISC,
    in `web/LICENSES.md`), drawn as `<svg class="ic"><use href="#i-name"/></svg>`. JS uses
    `dom.icon(name, cls)` or `ui.Icon`. `.ic` is 1.15em in `currentColor`, so an icon takes its
    button's size and colour; `fill` makes a solid shape. No glyph or emoji stands in for one,
    and a test checks every name used has a symbol (a missing one draws nothing).
  - Unsaved work shows three ways: Save enabled, "Edited" with the accent dot beside the set's
    name, and "• " leading the tab title. `history.syncSaveState()` sets all three from
    `S.dirty`, so `setDirty()` and `restore()` call it. After a save it reads "Saved just now",
    then "Saved N min ago" (`S.savedAt`, refreshed every 30s).
  - `activity.js` is work still running: `begin(kind, label)` returns `{ set, end }`, and
    `during()` wraps a promise. Tasks of one kind show as one chip, the oldest's label plus
    "· N queued", which is how several Shazam lookups waiting their turn on the server read. Save,
    Embed, uploads, the picker's lookup and the waveform each report there. Toasts are for things
    that finished: they stack (newest at the bottom, at most four, each on its own timer, sized to
    its text) just above the player bar, so a sample's answer and "Saved" no longer overwrite
    each other. `toast(msg, { kind: "error" })` adds the alert icon and stays longer.
  - Empty states: with nothing playing, the player shows the set (the episode title, the cover or
    the starred or first named track's art from `artwork.setArt()`) with "Space plays from the
    start", and dims the scrubber (`#player.idle`). The episode cover is a dashed drop target
    that says what Embed will use; the inspector and "Needs a look" say what goes in them.
  - The toolbar's ⌘K button looks like the search field it opens. Below the narrow breakpoint it
    keeps only its icon, and the activity area shows one task. `body` has one
    `minmax(0,1fr)` column, or an auto column grows to the toolbar's content and pushes Save off
    a narrow window.
- **Context menus and ⌘K (#59).** `commands.js`'s `COMMANDS` is every action the page offers,
  each `{ id, group, label, menu, keys, playhead, when, run }`, and `run` calls the same function
  the action's button, gesture or key already calls, so the three can't drift. `when(c)` returns
  `true` (offer it), `false` (leave it out: Unmerge on a kept track) or a string, which greys it
  out and is shown as the reason. The context `c` is `{ track, at, pointer, probe }`; `MENUS` lists
  which commands each surface offers (`block`, `row`, `pin`, `moment`), and a test checks every id
  there is a command.
  - **The menu** (`menu.js`) is a modal `<dialog>` with a clear `::backdrop`, so a click outside
    closes it without landing on the page, and the keymap stands down (`dialogOpen()`). The
    browser's own menu is prevented over `#tl-rows` and `#list` only, and never inside a text
    field (copy and paste). A right-click on the backdrop closes the menu and re-sends the
    `contextmenu` to `elementFromPoint()`, which reopens it there, as a desktop menu does.
    Right-clicking a row or block selects it and focuses its row first, so focus returns there
    when the menu closes, and the keys shown beside items apply. ⇧F10 and the menu key open the
    selected track's menu under its row (`selectedMenu()`); the menu key also fires a
    `contextmenu` afterwards, ignored for 400ms after a *keyboard* open (`menuJustOpened()`) and
    not after a mouse open, or a quick second right-click elsewhere would be swallowed.
  - Picks run in the dialog's `close` listener, after focus has gone back, so Rename and Insert
    keep the focus they move. `close` arrives a task after `close()`, by which time a right-click
    outside may already have opened the next menu, so the listener touches nothing but the pick.
  - In a row menu the moment is the playhead (Split, Identify), and Copy copies it while it's
    inside the track, else the track's start (`copyAt`).
  - **⌘K** (`palette.js`) lists every command, a track by artist or title, and "Go to" for a typed
    `1:23:45` or `75:30` (which seeks and selects the track there). Ranking is `TL.fuzzy`
    (Node-tested): a query word found whole scores far above the same letters scattered, so
    "luvm" puts The Luvmenauts above a command that only contains l…u…v…m. Commands marked
    `playhead` (S, I) act on the track under the playhead, as their keys do. ⌘K opens from
    inside a text field and does nothing while another dialog (the picker) is open; ⌘K again
    closes it, stopped from bubbling as the `?` sheet's `?` is.
- **The keyboard map (#58).** `keys.js`'s `KEYMAP` is one table of `{ group, keys, label, match,
  need, run }`, and both the dispatcher and the `?` sheet (`#keys-dialog`, built on first open)
  read it, so the sheet can't drift; mouse gestures are the static `MOUSE` list beside it, which
  replaced the prose hint under the timeline. The first entry that matches wins (⇧Space above
  Space), `need` gates on the player or a selection, and a `run` returning `false` lets the key
  through. ↑/↓ move the **selection**, not the audio. Enter/F2 rename, ⌫ / ⇧⌫ merge into the
  previous / next track with the inspector's gates (on a merged row, ⌫ brings it back), ←/→ seek
  5s (⇧ 15s, ⌥ 1s), J/K/L shuttle (no reverse in a browser, so J slows a fast shuttle or steps
  back 5s; any pause drops to 1x), F follows the playhead.
  - The guard order matters: any open dialog owns the keyboard (`dialogOpen()`); then ⌘K and
    ⌘S, *before* the input guard, since a field holds a draft until it blurs (the list commits on
    a timeout), so ⌘S blurs it and saves on the next tick; then the guard, which checks the
    event's **target** as well as the focus, because a field's Enter can move focus to its row
    before the event bubbles here, and would then rename that row again; then ⌘Z, past the
    guard so a field keeps its own undo. Space and Enter yield to a focused button.
  - The list is a `listbox` of `option` rows with a roving `tabindex`: the selected row (or the
    first) is the one tab stop, and the rows' buttons are out of the tab order. Keyboard
    selection focuses the row (`focusSelected()`, `preventScroll`) and scrolls with
    `revealSelected()`'s `block:"nearest"`; rows have `scroll-margin-top` for the sticky header.
    `load()` puts focus back on the selected row when a reload remounted it under the user.
  - Two row markers: the selection is the background (deeper while the list has focus), and
    playing is the accent bar on the left edge.
- **The app shell (#54).** The body is a full-window CSS grid: toolbar, timeline, divider, then
  the tracklist beside the inspector, then the player as an ordinary grid row. `html` and `body`
  are `overflow:hidden`; only the panes scroll. `shell.js` owns the inspector's tabs (Track, Set,
  Needs a look), the two dividers and the narrow-window slide-over. Pane sizes are the `--tl-h`
  and `--side-w` custom properties, saved in `localStorage` (inside try/catch) and `clamp()`ed by
  the grid, so a size saved on a big screen cannot push a pane off a small one.
  - The tracklist is a CSS-grid table. The column header `#list-head` is static markup *beside*
    `#list`, because the Preact render owns all of `#list`. Rows and header share the `.trow`
    template. `.meta` is `display:contents`, so artist and title are two grid cells.
  - Clicking a row selects it; clicking its artist or title also edits that cell in place.
    Selecting shows the Track tab, except from "Needs a look" (`keepTab`), where the list and
    timeline already show the choice. `splitAt()` shows the Track tab too, since its new fields
    are where the new track gets named, and a field in a hidden tab can't take focus.
  - The description lives in the Set tab. `autosizeSummary()` skips a hidden textarea, which
    measures 0, and `showTab("set")` sizes it.

### `setlist_maker/web_timeline.js` + the editor's timeline

- **One zoomable row (#57).** The timeline is one row over the whole set, not a row per hour.
  `grid.js` holds `view = { start, span }`, and every position goes through it: blocks, pins,
  the ruler, the playhead, the hover cursor, a click, a double-click, ⌥-click and the edge drag
  (`timeAt()` / `tAtX()`). ⌘-scroll or a pinch (a wheel event with `ctrlKey`) zooms around the
  pointer, and scrolling pans. `Z` fits the selected track, `0` the whole set, `=`/`-` zoom
  around the middle. It goes from the whole set down to `TL.MIN_SPP` (0.05 s/px), and
  `TL.clampView` / `zoomAt` / `tickStep` are the pure, Node-tested maths. Selecting a track
  pans the view to it, at the same zoom, when none of it shows. Playback that runs off the
  right edge of a view it was inside pages the view on; a view elsewhere is left alone.
  - `paintView()` is everything that moves with the view and runs once per animation frame
    while panning (`requestPaint()`). `renderTimeline()` is for model changes: it also redraws
    the overview strip, the stats, the inspector and the queue. Measured on the 4-hour set,
    a pan frame is ~8ms.
  - The overview strip `#tl-mini` is a canvas: each kept track's colour along the top, the
    whole set's waveform under it, and `#tl-mini-box` showing the view. Pressing outside the box
    centres the view there; dragging moves it; double-click fits the set.
  - The waveform is a canvas behind translucent blocks (`#tl-wave`). `TL.peakLevels` halves the
    peaks repeatedly and `TL.peakColumns` reads about one pair per device pixel, so any view
    costs about the same. Its colour is the `--wave` token, read from the stylesheet and
    re-read when the colour scheme changes. Canvases sit in static markup, outside the Preact
    roots (`#tl-ruler`, `#tl-blocks`, `#tl-pins`), so a render never resets them.
  - A block wide enough shows its name, and wider still its artwork (capped at 56px). The old
    art lane, and the thumbnails that overlapped and scaled up on hover, are gone.
  - The player's scrubber has the same waveform for the playing track's window on a canvas
    above the native range input (`#seek-wave`), the played part in the accent colour.

- **The tracklist is the truth; probes are evidence laid over it.** The grid's blocks come from
  `tracks[]` (each track's window is the player's own `windowFor()` rule, via `Timeline.windows`)
  and each probe from `GET /api/probes` is joined to whichever window holds its *midpoint*. It
  never draws from the engine's `segments()` in edit mode: the first merge or split would make
  the two disagree, and the `.md` is what gets saved. Merge, split and "Use this name" are the
  existing mutations (`rejected`, an inserted `_new` row, `_edited` artist/title), so the save
  payload, `apply_track_edit()`'s correction learning and the list all work unchanged.
  "Merge into previous" **is** reject: `to_markdown()` drops rejected rows, so the span goes to
  the kept track before it (`Timeline.keptWindows`). A kept track owns its kept window
  *everywhere* -- the grid draws one block per kept window (a merged row survives only as a
  seam, deliberately not a click target: merged blips sit seconds apart, a pixel or two at the
  grid's scale, so the kept track's inspector lists them instead, each with an Unmerge), the
  inspector reports that span, and
  `joinProbes` pools the merged rows' probes into it. Drawing per-row windows instead left a
  merge looking undone. Evidence heard inside a merged *different* track's span is not then
  flagged as a rival name: the merge settled it (on the real set, Parov Stelar twice at 0.99
  inside Computer Love's blips would otherwise appear the moment the blips were merged).
- **Merging into the next track (#56)** is a rejected row marked `merged: "next"`. The next kept
  track's start is *derived*, not written into the page: `keptWindows` starts it at the first
  such row in the run of rejected rows before it. In a mixed run, rows go to the earlier track up
  to that row and to the later one from there, so both windows stay contiguous. A "next" row
  with no kept track after it stays with the one before. Deriving the start, rather than
  setting the next track's timestamp, keeps `clampStart`'s rule that no two rows share a second
  inside the page, and makes Unmerge just clearing the flags. Everything that moves a start
  (the dragged edge, `[`/`]`, both Start fields) moves `TL.startIndex()`'s row, the one holding
  the visible start. The save sends each kept track's derived start, so the server moves it as
  in #55, and the page reloads. After that reload the rejected row really does share the next
  track's second, which is why `clampStart` never pushes a row past a neighbour it already ties,
  and why `edits.mergeTrack()` refuses to bring that row back until the track has moved off it.
  `splitAt()` refuses a cut inside the span merged in from before (and the Split button is
  disabled there): the new row would land between the merged row and the track, take the merged
  span over, and the cut would show nowhere. "＋ Add below" on a merged row does take its span,
  which is what adding below that row means.
  `mergeTrack()` is the one place a row is merged or brought back (the inspector, the list's ✕
  and ↩, Needs a look), and `TL.owners()` says which kept track holds each row, for the
  inspector's "Merged into this track" and the variant rule. The short-track issue offers Merge
  down for the first track, Merge up for the last, and in between picks by evidence: the
  neighbours' names heard inside the short window, plus its own name heard inside theirs. A tie
  keeps Merge up, and the other direction is a second button.
- **Pure, and tested under Node.** `web_timeline.js` holds every derivation (`identity`,
  `cleanTitle`, `keptWindows`, `owners`, `startIndex`, `joinProbes`, `variants`, `unheard`, `findIssues`,
  `snapPoints`) with no DOM, served at
  `/timeline.js` and `require`d by `tests/test_web_timeline.py`, which skips when `node` is
  missing. Issue rules: an **A x y A** run where every interloper is under `BLIP_SECONDS` is one
  merge (the real 09-23 set's *Computer Love* split by 11s/3s/8s blips); a lone track under
  `SHORT_SECONDS`; unidentified stretches; remaster tags as one batched "Tidy"; stretches over
  `UNHEARD_SECONDS` no probe heard; and **name variants**, which count only a rival heard twice
  or under the *same artist* (Trio's German/English *Da Da Da*), compared by `identity()` so a
  tidied title is not a rival of Shazam's "(2009 Remaster)" spelling. A lone stray from another
  artist is noise every long track collects -- the inspector still lists it. There is
  deliberately no "heard only once" rule: 0.66 is ordinary Shazam confidence on these sets.
- `cleanTitle` strips reissue bookkeeping only; "(Vocal)" and "(Maxi Version)" name a different
  recording and must survive.
- **Undo/redo** (header buttons, ⌘Z / ⇧⌘Z) is recorded in `setDirty(true)`, the one call every edit
  path already ends in, so a new mutation cannot forget it. A step is the list's order plus a
  copy of each track's fields (plus the unsaved episode-cover choice, `coverChoice`), restored
  onto the **same objects** so the `selectedTrack` /
  `playingTrack` / `artTrack` references stay valid. It is taken in a microtask, which makes
  one user action one step (a Tidy retitling eight tracks, a commit that renames and
  re-sorts), and a step that changed no track is dropped, which keeps description typing out
  of it -- the textarea has its own undo, and the shortcut sits past the input guard so ⌘Z
  in a field stays the field's. Saving clears the history: inserted rows get real indices
  then, and undoing past that would re-send one as new.
- **Moving a start (#55)** goes through `model.clampStart(i, seconds)`: whole seconds, at least
  1s from the rows on either side, *rejected rows included*, so no move ever reorders the list.
  A reorder would re-home merged rows silently, and two tracks sharing a timestamp would share
  one sidecar entry, since the sidecar is joined to the `.md` by timestamp and the last match
  wins. An existing row's typed start is clamped the same way; only an *inserted* row's is
  sorted into place. `edits.moveStart()` is the one recorded move, used by the `[`/`]` keys,
  the inspector's Start field and the end of a drag.
  - **Dragging.** Each kept block has a `.tl-edge` handle on its left edge while its start is
    in view (`.tl-seam` is a merged row's marker and takes no pointer events). `pointerdown`
    captures on `#tl-rows`, not the handle: the handle is dropped when the boundary leaves the
    view, and capture would go with it. A start outside the view is reached by zooming out
    first; the drag does not pan. The drag previews with
    `previewStart()` (unrecorded) once per animation frame, then puts the start back and commits
    with `moveStart()` on release, so one drag is one undo step. Escape cancels. Release plays
    from 5s before the new cut.
  - It snaps within 6px to `TL.snapPoints(probes)`: between two neighbouring identified probes
    with different identities, the midpoint of the gap between their centred 10s excerpts. ⌥
    turns snapping off, which is why the click handler ignores `.tl-edge`: otherwise a ⌥-drag
    ends in a ⌥-click, which splits. Snapping to quiet points in the waveform's peaks is not
    built yet.
- **Splitting** is one function, `splitAt(track, seconds)`, reached three ways: the inspector's
  button (selected track, playhead), **S** (whichever kept track holds the playhead -- automatic
  advance does not select, so the selection may be elsewhere) and **Alt-click** on a block (that
  block, pointer position). S focuses the new row (not its fields), so Space and the arrows keep
  driving playback and Enter names it.
  The new row is inserted in timestamp order, since a kept window runs over merged rows.
- **Identify at playhead** (inspector button, **I**) is `identifyAt(track, seconds)`: a
  `sampleAt()` centred on the playhead, then `setNames()` -- an ordinary, undoable edit. The track
  is captured by reference *before* the request, since the answer can wait out a pacing
  interval. Unidentified: filled in. Same `identity()`: "Shazam agrees", no edit. Named
  otherwise: an `ask()` first (`confirmReplace()`: both names, the time, the confidence, and ▶
  Listen to hear the fingerprinted ten seconds; closing stops it and puts the playhead back),
  because Shazam is often wrong mid-mix and the name may be the user's own correction; declining
  leaves the answer in "Shazam heard" with its Use button.
  Live mode only samples: nothing may be edited under the run.
- Selection is an object reference (`selectedTrack`), like `playingTrack`/`artTrack`. Choosing
  to play a track selects it; automatic advance does not, unless **Follow** is on (`S.follow`, F,
  the player bar's toggle, kept in `localStorage`), and never while a text field has focus, or
  the inspector would change track under the typing. During a replay the inspector and the
  queue skip the redraws: they describe every sample, not the replay's subset.

### `setlist_maker/sampler.py` - Click-to-sample

- **ShazamSampler:** one lookup at a moment the user picked. The window is *centred* on the click
  (`sample_window_start`), because shazamio fingerprints the middle 10s of what it is handed.
  Slices with pydub's `start_second`/`duration` so ffmpeg seeks: never `load_audio()` in the
  editor, four hours of PCM is ~2.5GB. Serialized on a lock and spaced `DEFAULT_DELAY_SECONDS`
  apart, including after a failure, since the limit is burst-sensitive. The answer is a `Probe`
  with purpose **"manual"**, appended to `_progress.json` by `adaptive.append_probe()` (creating
  a v2 file, or converting a legacy one, as a resume would), so a later resume replays it as
  evidence. To the engine "manual" is plain evidence: only "coverage" anchors, only "refine"
  counts toward the thrash cap. These calls do **not** reach the call log.
- **SampleRequests:** during a live run the run is the only Shazam caller -- two would double the
  burst and race on the progress file -- so a click is queued, the adaptive driver takes it
  ahead of `engine.next_probe()` inside its normal pacing, saves it, and only then resolves the
  waiting HTTP thread (which re-reads the file for its pin). `close()` fails waiters and
  refuses new asks; the driver closes it when its loop ends.
- `save_progress_v2()` now writes a sibling `.tmp` and `os.replace`s it: the watch page re-reads
  that file while the run is writing it.

### `identify --watch` - Live timeline (`web_editor.WatchSession` / `LiveRun`)

- Opens the editor's server in a background thread **before** the run, in live mode. The page
  follows the run through the progress file alone: `adaptive.live_snapshot()` replays it through a
  fresh engine -- resume is replay, so that *is* the run's state, and the same `segments()` ->
  `results_to_tracklist(deduplicate=False)` shaping it finishes with. `LiveRun` caches the
  replay on the file's (mtime, size); ~0.55s for 339 probes, once per new probe. `/api/probes`
  adds `live` and `next` (the pulsing "listening here next" pin).
- **Read-only while live.** `/api/save` answers 409 and the page disables every edit
  (`setDirty` refuses, `body.live` hides the controls), because `finalize_outputs()` writes the
  tracklist over anything saved mid-run. Sampling is the exception, routed via `SampleRequests`.
- **Same tab becomes the editor.** `finish()` fills in the finished tracklist, corrections, audio
  and sampler, and clears `live` *last*, since that is what flips the handler. The page sees
  `live: false` on its next 3s poll and reloads. Done during the run sets `ctx.closed`, and
  `finish()` then returns instead of waiting on a server that is gone. A failed or aborted run
  calls `stop()`. `--watch` on a set that already has a tracklist just opens `--web-edit`; a
  `--sequential` run is watched without click-to-sample (nothing can take a question between
  its samples). Exclusive with `--edit`.

### `setlist_maker/progress.py` - Live progress panel for the identify run

- **render_panel():** The dashboard pinned under the scrolling per-sample log — a boxed
  four-line readout whose **title and border colour *are* the phase**, so the state
  registers before a word is read. A fixed right-hand rail carries position, confidence,
  elapsed and ETA in the same cells every frame. Pure function of `RunState` plus a clock,
  so it unit-tests at a fixed width with no `Live`, no pty and no timing — exactly as
  `identify.format_progress_line()` does, and for the same reason.
- **Everything that moves is derived from the clock, not a counter the pipeline ticks.**
  `Live` re-renders on its own thread, so a renderable recomputing from `time.monotonic()`
  animates the cooldown countdown *during* `await asyncio.sleep(delay_seconds)` — which is
  why that sleep in `process_single_file` needed no restructuring (measured: 11 renders in
  an untouched 1.0s sleep). `RunState.started_at` therefore reads from the **injected**
  `clock`, not from `time.monotonic` directly, or a fake clock mixes with a real start.
- **The panel's height must never change.** `Live` erases a fixed number of lines, so a
  seventh row corrupts the redraw. Two things defend that: `_one_line()` collapses
  whitespace in every user-supplied field (one newline in a Shazam title is enough), and
  every glyph is deliberately one cell wide — no ⏳/⌛, since a glyph rich measures as 2 and
  the terminal draws as 1 bends the right border on every redraw.
- **live_display():** Wraps both paths — panel, or plain stdout — behind one object, which
  is what lets the identify loop stay a single code path with no `if live:` inside it.
  `redirect_stdout` stays on (it is what puts `shazam_client`'s surviving `print()`s
  *above* the panel); `redirect_stderr` is explicitly **off**, because rich redirects
  stderr whenever *stdout* is a terminal and `identify set.mp3 2> errors.log` would
  otherwise capture nothing and dump every warning onto the terminal.
- The rail widens past `RAIL` when the recording needs it (a 10-hour file wants
  `5:00:00 / 10:00:00`): the rail exists to hold the position steady, so widen rather than
  ellipsize the one number it is there to show.
- **`AdaptiveRunState` / `render_adaptive_panel()`** are the adaptive run's twin, under the same
  fixed-height and one-cell-wide discipline, and `ProgressPanel` / `live_display()` take a
  `render` parameter so the sequential call sites pass nothing and behave identically. The twin
  is deliberate rather than a shared base: `RunState` has non-default fields, so a
  default-bearing mixin cannot slot under it without making the whole sequential path
  keyword-only. What differs is the numbers — there is no fixed total to count towards, so the
  bar's denominator is `probes_done + est_probes_remaining` and moves as the engine learns, and
  the stats row reports boundaries sharp and the widest remaining gap instead of a sample count.
- Gated by `identify --no-panel`, and skipped automatically when stdout is not a terminal.
  Note the two questions are **separate**: a terminal gets the colorized log either way, so
  `--no-panel` drops only the dashboard, never the log's own rendering.
- Known gap: `Live` restores the cursor on normal exit and on **Ctrl-C** (verified), but a
  SIGTERM/SIGHUP kill unwinds nothing and leaves the cursor hidden until `reset`.

### `setlist_maker/call_log.py` - Per-call telemetry for an identify run

- **The problem it exists for:** the pipeline **cannot tell a rate-limited sample from a
  genuinely unidentified one**, and never could. shazamio's own constructor sets
  `ExponentialRetry(attempts=20, max_timeout=60, statuses={500,502,503,504,429})`, so a 429
  is retried *inside* one `await recognize()` — >700s of backoff before anything reaches the
  caller — and then arrives in one of three shapes, none of them detectable: a JSON error body
  returned as a plain dict (no `"track"` key → `None`), an empty body returned as `None`, or
  `FailedDecodeJson("Failed to decode json")`, a frozen literal carrying no status. That last
  one is the shape Shazam actually serves (a ~142-byte `text/html` block page), and it means
  `shazam_client.py`'s `"429" in error_str or "too many" ... or "rate" ...` test **never fires**
  — the backoff/`on_backoff`/yellow-panel path is dead code. A throttled run therefore produces
  a confident-looking tracklist with silent holes, and `progress.json` cements them, since a
  resume treats a throttle-`None` as a finished sample.
- **`CallRecorder`:** subscribes to shazamio's `http_client.trace_config.on_request_end`.
  shazamio builds that `TraceConfig` and passes it to every request but only ever subscribes
  `on_request_start`; `on_request_end` is free and is handed the live `ClientResponse`. So this
  sees **every** HTTP attempt's status, headers and attempt number — including the 429s
  shazamio retried away, which is the one fact nothing else in the stack can observe — while
  changing no behaviour and replacing no client. Verified against a loopback server:
  two absorbed 429s, a 200, and the caller still gets its normal match. `attach()` is guarded
  and returns False rather than raising: `trace_config` is shazamio's internal detail, and
  losing the log is acceptable where failing the run is not. `test_shazamio_still_exposes_the_trace_seam`
  is the canary — it fails loudly on an upgrade that moves the seam instead of logging nothing
  quietly. Prefer this over the `Shazam(http_client=...)` injection seam **for observation**:
  that one is supported and sees everything too, but it means owning a mirrored `request()`
  and a retry policy, i.e. changing behaviour while trying to measure it.
- **`describe_error()`:** classifies by exception **type**, never by substring. Two independent
  reasons: the rate-limit message contains no status at all, and a production tracker that
  grepped raw output for `429` misclassified real matches whose track ids contained those
  digits (9 in 100). The status and `Retry-After` survive only on `exc.__cause__` — aiohttp's
  `ContentTypeError`, preserved because `utils.py` re-raises `from e` — and only on the
  non-JSON branch. Shazam sends no `Retry-After` in practice; the field is recorded because
  its *absence* is itself worth confirming from real runs.
- **`CallLog`:** append-only JSONL, one `run` header plus one `call` line per sample. Opens and
  closes per line rather than holding a handle for the hour a run takes, so an interrupted run
  leaves a complete readable file. Every write is best-effort in the spirit of `summary.py`: a
  fault warns **once** and disables the log for the rest of the run — telemetry that can fail a
  run is worse than no telemetry.
- **`throttled`** is the field a review scans for, so it is true if a 429 appears in the attempt
  list **or** in the error's status. Attempts alone is not enough: if the trace seam ever moves,
  `attempts` comes back empty and the chained cause is the only surviving evidence.
- One shared `setlist-maker-calls.jsonl` beside the audio (or in `--output-dir`), **not** a
  per-file name: the point is to review several ordinary runs together. On by default
  (`--no-call-log` to disable, `--call-log PATH` to relocate; the off switch outranks an
  explicit path) — a log that must be enabled per run is empty exactly when a throttling
  question comes up.
- **Deliberately changes no behaviour.** No pacing, no retry policy, no adaptive delay. Field
  evidence puts the safe rate at 4-6 requests/min and the burst threshold at ~15-20 quick ones,
  so `DEFAULT_DELAY_SECONDS = 15` is already at the conservative end of what comparable clients
  ship (SongRec defaults to 10s) — there is little headroom, and the limit is burst-sensitive
  rather than average-sensitive, which makes AIMD and token buckets the wrong shape. Fixing the
  dead backoff path or taking over shazamio's 20 hidden retries would also mean the logs
  describe a system that was never run. Measure first.

### `setlist_maker/playback.py` - Editor audio preview
- **PlaybackController:** Drives a non-blocking `ffplay` subprocess (`play()` / `stop()` /
  `is_playing()` / `elapsed()`), reaping the child on stop. Deliberately out-of-process: an
  earlier in-process (`sounddevice`) version locked up Textual's event loop and was removed
- **playback_available():** Capability gate — `ffplay` on PATH **and** macOS. `audio_output_available()`
  is Darwin-only (playback is unsupported elsewhere; SSH is not special-cased). `PREVIEW_SECONDS = 30`
- Seek/scrub within a track is a tracked follow-up (GitHub issue #10)

Key classes:
- `Track`: Dataclass representing a single track with timestamp, artist, title, rejected status
- `Tracklist`: Collection of tracks with markdown/JSON export methods

### The JSON sidecar's shape

`Tracklist.to_json()` returns a **bare list** and must keep doing so. The curated
episode-cover choice is conceptually a property of the *set*, but it is stored as a flag on the
one track whose art is used precisely so the top level never changes: wrapping the list in an
object does not raise for existing readers, it degrades **silently** —
`_load_tracklist_with_artwork_urls()` iterates the loaded JSON, iterating a dict yields its keys
as strings, `"timestamp" in "tracks"` is a false substring test rather than a `TypeError`, and
the loader's `except` never fires. Every track would quietly lose its `coverart_url`, re-key the
artwork cache and re-fetch. The list is also joined back to the markdown **by timestamp**, not
by position (`to_json()` drops rejected tracks, so positions do not line up), which is why a
curated choice is a per-track flag rather than an index into anything.

### Test network guard

`tests/conftest.py` has an autouse fixture that fails any test resolving a non-loopback host.
It raises `NetworkAccessBlocked`, deliberately a **`BaseException`**: every artwork source
helper wraps its request in `except Exception` and returns `None`, so an ordinary error would be
swallowed and the unpatched test would pass quietly with no artwork — the exact silence the
guard exists to end. Patch seams instead: `setlist_maker.artwork.urllib.request.urlopen` for a
source helper, or `fetch_artwork` / `artwork_candidates` **in the module that imported it**.

### Test subprocess guard and server shutdown

Beside the network guard, `block_remote_subprocesses` swaps `subprocess.Popen` for a subclass
that raises `SubprocessBlocked` (also a `BaseException`, because `generate_summary()` catches
`Exception`) when the program is `claude`. The DNS guard cannot see that call: the lookup
happens in another process. `process_single_file()` defaults to `summary=True`, so a test that
drives it must pass `summary=False`. ffmpeg, ffprobe and node are local and run normally.

`fast_server_shutdown` makes `socketserver.BaseServer.serve_forever` default to a 50ms poll.
Patched on the base class so it also reaches the thread production `WatchSession` starts;
at 0.5s, waiting out `shutdown()` was ~75% of the suite's wall clock. `pytest-timeout`
(`timeout = 120` in `pyproject.toml`) turns a hang into a failure.

## Code Style

- Line length: 100 (configured in pyproject.toml)
- Ruff lint rules: E, F, W, I (errors, pyflakes, warnings, isort)
- Python 3.10+ required
