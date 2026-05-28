"""Track identification pipeline: Shazam batch processing, dedup, and output."""

import asyncio
import json
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from shazamio import Shazam

from setlist_maker.audio import (
    SAMPLE_DURATION_MS,
    format_timestamp,
    load_audio,
    slice_audio,
)
from setlist_maker.editor import CorrectionsDB, Track, Tracklist
from setlist_maker.shazam_client import identify_sample_with_retry
from setlist_maker.summary import generate_summary

DEFAULT_DELAY_SECONDS = 15  # Pause between API calls

# Two normalized titles at or above this ratio are treated as the same track,
# provided the artists also match (see ARTIST_SIMILARITY_THRESHOLD). Tuned to
# merge metadata drift (remix/feat/edit tags, typos) without collapsing
# genuinely different songs.
SIMILARITY_THRESHOLD = 0.85

# The same track virtually always reports a (near-)identical artist, so the
# artist gate is strict. This is what keeps two *different* songs -- even by the
# same artist -- from being merged just because their titles look alike.
ARTIST_SIMILARITY_THRESHOLD = 0.9

# A track detected in only a single sample is kept only if Shazam was at least
# this confident; otherwise it is treated as a stray false positive.
SINGLETON_CONFIDENCE_KEEP = 0.6


@dataclass
class DedupConfig:
    """Tunable knobs for deduplicate_tracklist (exposed as CLI flags)."""

    title_threshold: float = SIMILARITY_THRESHOLD
    artist_threshold: float = ARTIST_SIMILARITY_THRESHOLD
    singleton_confidence_keep: float = SINGLETON_CONFIDENCE_KEEP
    smoothing: bool = True


# Tokens that describe a *version* of a track rather than its identity. Stripped
# before comparison so "Song" and "Song - Radio Edit" cluster together.
_VERSION_TOKENS_RE = re.compile(
    r"\b(radio edit|original mix|extended mix|club mix|remaster(?:ed)?|remix|"
    r"re-?edit|edit|version|vip|bootleg|mono|stereo|live|acoustic|instrumental)\b",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\b(feat\.?|ft\.?|featuring|with)\b.*", re.IGNORECASE)
_BRACKETED_RE = re.compile(r"[(\[{].*?[)\]}]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_text(text: str) -> str:
    """Lowercase and strip version/feature tags and punctuation for matching."""
    text = text.lower()
    text = _BRACKETED_RE.sub(" ", text)
    text = _FEAT_RE.sub(" ", text)
    text = _VERSION_TOKENS_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def _normalized_key(track_info: dict) -> tuple[str, str]:
    """Build a normalized (artist, title) key for fuzzy comparison."""
    return (
        _normalize_text(track_info.get("artist", "")),
        _normalize_text(track_info.get("title", "")),
    )


def _assign_cluster(
    key: tuple[str, str],
    clusters: list[tuple[str, str]],
    title_threshold: float,
    artist_threshold: float,
) -> tuple[str, str]:
    """Return the representative key for `key`, fuzzily matched against clusters.

    Greedy and order-preserving: a cluster matches when the artist is within
    `artist_threshold` and the title is within `title_threshold`; the first such
    cluster wins, otherwise `key` becomes a new cluster representative.
    """
    artist, title = key
    for representative in clusters:
        rep_artist, rep_title = representative
        if (
            SequenceMatcher(None, artist, rep_artist).ratio() >= artist_threshold
            and SequenceMatcher(None, title, rep_title).ratio() >= title_threshold
        ):
            return representative
    clusters.append(key)
    return key


def _smooth_sequence(seq: list[tuple[str, str] | None]) -> list[tuple[str, str] | None]:
    """Flip an isolated single-sample outlier flanked by identical neighbors.

    Turns A?A into AAA, removing both transient mis-detections (A B A) and lone
    dropouts (A None A) that would otherwise fragment one long track.
    """
    smoothed = list(seq)
    for i in range(1, len(seq) - 1):
        left, mid, right = seq[i - 1], seq[i], seq[i + 1]
        if left is not None and left == right and mid != left:
            smoothed[i] = left
    return smoothed


def deduplicate_tracklist(
    raw_results: list[tuple[int, dict | None]],
    config: DedupConfig | None = None,
) -> list[tuple[int, dict | None]]:
    """
    Filter and deduplicate track matches.

    1. Fuzzy-cluster matches so metadata drift (remix/feat/edit tags, typos)
       for one track collapses to a single identity.
    2. Smooth isolated single-sample outliers (A B A / A None A -> A A A).
    3. Drop singletons unless Shazam was confident (a real short track).
    4. Collapse consecutive identical matches, preserving unidentified gaps.
    """
    if config is None:
        config = DedupConfig()

    # 1. Assign every identified sample to a fuzzy cluster, remembering the
    #    highest-confidence metadata seen for each cluster as its representative.
    clusters: list[tuple[str, str]] = []
    cluster_meta: dict[tuple[str, str], dict] = {}
    timestamps: list[int] = []
    seq: list[tuple[str, str] | None] = []

    for timestamp, track_info in raw_results:
        timestamps.append(timestamp)
        if not track_info:
            seq.append(None)
            continue

        representative = _assign_cluster(
            _normalized_key(track_info),
            clusters,
            config.title_threshold,
            config.artist_threshold,
        )
        seq.append(representative)

        # Keep the highest-confidence metadata as the cluster's representative;
        # on ties the first-seen variant wins (usually the cleanest title).
        confidence = track_info.get("confidence") or 0
        best = cluster_meta.get(representative)
        if best is None or confidence > (best.get("confidence") or 0):
            cluster_meta[representative] = track_info

    # 2. Smooth transient outliers.
    if config.smoothing:
        seq = _smooth_sequence(seq)

    # 3. Confidence-aware singleton removal.
    counts = Counter(rep for rep in seq if rep is not None)
    for i, rep in enumerate(seq):
        if rep is not None and counts[rep] == 1:
            confidence = cluster_meta[rep].get("confidence") or 0
            if confidence < config.singleton_confidence_keep:
                seq[i] = None

    # 4. Collapse consecutive identical clusters, preserving unidentified gaps.
    tracklist = []
    last_rep = None
    pending_unidentified = None

    for timestamp, rep in zip(timestamps, seq):
        if rep is None:
            if last_rep is not None and pending_unidentified is None:
                pending_unidentified = timestamp
            continue

        if rep != last_rep:
            if pending_unidentified is not None:
                tracklist.append((pending_unidentified, None))
                pending_unidentified = None

            tracklist.append((timestamp, cluster_meta[rep]))
            last_rep = rep

    if pending_unidentified is not None:
        tracklist.append((pending_unidentified, None))

    return tracklist


def results_to_tracklist(
    raw_results: list[tuple[int, dict | None]],
    source_filename: str,
    corrections_db: CorrectionsDB | None = None,
    dedup_config: DedupConfig | None = None,
) -> Tracklist:
    """
    Convert raw Shazam results to a Tracklist object.
    Applies corrections from the database and deduplicates.
    """
    # Apply corrections before deduplication
    if corrections_db:
        corrected_results = []
        for timestamp, track_info in raw_results:
            if track_info:
                correction = corrections_db.get_correction(
                    track_info["artist"], track_info["title"]
                )
                if correction:
                    track_info = track_info.copy()
                    track_info["original_artist"] = track_info["artist"]
                    track_info["original_title"] = track_info["title"]
                    track_info["artist"], track_info["title"] = correction
            corrected_results.append((timestamp, track_info))
        raw_results = corrected_results

    # Deduplicate
    deduped = deduplicate_tracklist(raw_results, dedup_config)

    # Convert to Track objects
    tracks = []
    for timestamp, track_info in deduped:
        if track_info:
            track = Track(
                timestamp=timestamp,
                artist=track_info.get("artist", ""),
                title=track_info.get("title", ""),
                shazam_url=track_info.get("shazam_url"),
                album=track_info.get("album"),
                coverart_url=track_info.get("coverart_url"),
                confidence=track_info.get("confidence"),
                original_artist=track_info.get("original_artist"),
                original_title=track_info.get("original_title"),
            )
        else:
            track = Track(timestamp=timestamp, artist="", title="")
        tracks.append(track)

    return Tracklist(
        source_file=source_filename,
        tracks=tracks,
        generated_on=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def save_progress(results: list, filepath: Path):
    """Save intermediate results to JSON in case of interruption."""
    # Convert to serializable format
    serializable = [(ts, info) for ts, info in results]
    with open(filepath, "w") as f:
        json.dump(serializable, f, indent=2)


def load_progress(filepath: Path) -> list:
    """Load previous progress if it exists."""
    if filepath.exists():
        with open(filepath, "r") as f:
            return json.load(f)
    return []


async def process_single_file(
    audio_path: Path,
    output_dir: Path | None,
    delay_seconds: int,
    resume: bool = True,
    corrections_db: CorrectionsDB | None = None,
    dedup_config: DedupConfig | None = None,
) -> tuple[Tracklist, Path] | None:
    """
    Process a single audio file and generate its tracklist.
    Returns (Tracklist, output_path) on success, None on failure.
    """
    print(f"\n{'=' * 60}")
    print(f"Processing: {audio_path.name}")
    print(f"{'=' * 60}")

    # Set up output paths
    base_name = audio_path.stem
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{base_name}_tracklist.md"
        progress_path = output_dir / f"{base_name}_progress.json"
    else:
        output_path = audio_path.parent / f"{base_name}_tracklist.md"
        progress_path = audio_path.parent / f"{base_name}_progress.json"

    # Load audio and create slices
    try:
        audio = load_audio(audio_path)
    except Exception as e:
        print(f"  Error: Failed to load audio: {e}")
        return None

    slices = slice_audio(audio, SAMPLE_DURATION_MS)

    # Check for existing progress
    raw_results = []
    start_index = 0
    if resume and progress_path.exists():
        raw_results = load_progress(progress_path)
        start_index = len(raw_results)
        if start_index > 0:
            print(f"  Resuming from sample {start_index + 1} ({start_index} previous results)")

    # Initialize Shazam
    shazam = Shazam()

    # Process each slice
    total_slices = len(slices)
    with tempfile.TemporaryDirectory() as temp_dir:
        for i, (timestamp, segment) in enumerate(slices[start_index:], start_index + 1):
            time_str = format_timestamp(timestamp)
            print(f"  [{i}/{total_slices}] Sample at {time_str}")

            track_info = await identify_sample_with_retry(shazam, segment, temp_dir)

            if track_info:
                print(f"  Found: {track_info['artist']} - {track_info['title']}")
            else:
                print("  Not identified")

            raw_results.append((timestamp, track_info))

            # Save progress after each sample
            save_progress(raw_results, progress_path)

            # Delay before next request (except for the last one)
            if i < total_slices:
                await asyncio.sleep(delay_seconds)

    # Convert to Tracklist with corrections applied
    print("\n  Processing complete. Generating tracklist...")
    tracklist = results_to_tracklist(raw_results, audio_path.name, corrections_db, dedup_config)

    # Add a one-paragraph playlist description ahead of the listing. Warns and
    # continues if the Claude CLI is unavailable or the call fails.
    print("  Generating playlist summary...")
    summary_lines = [
        f"{t.artist} - {t.title}"
        for t in tracklist.tracks
        if not t.rejected and not t.is_unidentified
    ]
    tracklist.summary = generate_summary(summary_lines)

    # Write markdown plus a JSON sidecar. The JSON carries each track's
    # Shazam cover-art URL, which the chapters command relies on, so it is
    # always written here -- not only when the editor saves.
    with open(output_path, "w") as f:
        f.write(tracklist.to_markdown())

    json_path = output_path.with_suffix(".json")
    with open(json_path, "w") as f:
        json.dump(tracklist.to_json(), f, indent=2)

    print(f"  Saved: {output_path}")
    print(f"  Found {len(tracklist.tracks)} unique tracks")

    # Clean up progress file
    if progress_path.exists():
        progress_path.unlink()

    return tracklist, output_path
