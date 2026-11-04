"""M3's end-to-end check: score identification runs against a corrected tracklist.

Each run is a ``_progress.json``, folded by Python's engine (M2 proved Hindsight folds it
to the same segments) into the tracklist the run would write, with **no** corrections
applied, so neither run benefits from names learned from the truth. A truth track is found
when a run has a track of the same identity (``identity.py``'s clustering, as the engine
uses it) starting within ``--window`` seconds of it; its boundary error is the distance
between the two starts. A run's tracks that match nothing in the truth are listed, not
counted as errors: the truth was corrected from an earlier Python run, so it may be missing
what a different run heard.

    uv run python scripts/score_m3.py <truth_tracklist.md> <run_progress.json> ...
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from setlist_maker.adaptive import live_snapshot
from setlist_maker.identify import ARTIST_SIMILARITY_THRESHOLD, SIMILARITY_THRESHOLD
from setlist_maker.identity import assign_cluster, normalized_key
from setlist_maker.tracklist import Track, load_tracklist


def same(a: Track, b: Track) -> bool:
    clusters: list[tuple[str, str]] = []
    first = assign_cluster(
        normalized_key({"artist": a.artist, "title": a.title}),
        clusters,
        SIMILARITY_THRESHOLD,
        ARTIST_SIMILARITY_THRESHOLD,
    )
    second = assign_cluster(
        normalized_key({"artist": b.artist, "title": b.title}),
        clusters,
        SIMILARITY_THRESHOLD,
        ARTIST_SIMILARITY_THRESHOLD,
    )
    return first == second


def fmt(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def score(truth: list[Track], run: list[Track], window: float) -> dict:
    errors: list[float] = []
    missed: list[Track] = []
    used: set[int] = set()
    for t in truth:
        candidates = [
            (abs(r.timestamp - t.timestamp), i)
            for i, r in enumerate(run)
            if not r.is_unidentified and abs(r.timestamp - t.timestamp) <= window and same(t, r)
        ]
        if candidates:
            err, i = min(candidates)
            errors.append(err)
            used.add(i)
        else:
            missed.append(t)
    extra = [r for i, r in enumerate(run) if i not in used and not r.is_unidentified]
    return {"errors": errors, "missed": missed, "extra": extra}


def main() -> None:
    ap = argparse.ArgumentParser(description="Score identification runs against a tracklist.")
    ap.add_argument("truth", type=Path)
    ap.add_argument("runs", type=Path, nargs="+")
    ap.add_argument("--window", type=float, default=120.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    truth = [t for t in load_tracklist(args.truth).tracks if not t.is_unidentified]
    print(f"truth: {args.truth.name}, {len(truth)} identified tracks\n")
    for path in args.runs:
        tracklist, _plan, probes, _duration = live_snapshot(path, path.name)
        tracks = tracklist.tracks
        r = score(truth, tracks, args.window)
        errs = sorted(r["errors"])
        print(f"{path.name}: {len(probes)} probes, {len(tracks)} tracks")
        print(f"  found {len(errs)} of {len(truth)}")
        if errs:
            p90 = errs[min(len(errs) - 1, int(0.9 * len(errs)))]
            print(
                f"  boundary error: median {statistics.median(errs):.1f}s, "
                f"p90 {p90:.1f}s, max {errs[-1]:.1f}s; "
                f"within 5s {sum(e <= 5 for e in errs)}, within 15s {sum(e <= 15 for e in errs)}"
            )
        print(f"  missed: {len(r['missed'])}, not in the truth: {len(r['extra'])}")
        if args.verbose:
            for t in r["missed"]:
                print(f"    missed  {fmt(t.timestamp)}  {t.artist} - {t.title}")
            for t in r["extra"]:
                print(f"    extra   {fmt(t.timestamp)}  {t.artist} - {t.title}")
        print()


if __name__ == "__main__":
    main()
