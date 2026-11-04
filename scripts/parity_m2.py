"""M2's parity check: Python and Hindsight fold every run to the same bytes.

For each progress file, Python replays it into ``BoundaryEngine`` one probe at a time and
writes a JSON line after each (and one before the first): the events ``add_probe``
returned, ``segments()`` and its drops, ``next_probe()``, ``boundary_stats()``,
``estimated_probes_remaining()``, ``max_ratio`` and ``max_boundary_width``.
``hindsight replay`` does the same in Rust. The two outputs must be byte-equal. Resume is
replay, so equal lines at every prefix mean a run resumed by either one continues the same.

The corpus is every progress file under the given folders (the originals are only read)
and the engine's fixtures, plus ``--fuzz N`` generated cases:

- oracle runs (``tests/boundary_oracle.py``) to convergence or stopped early, across seeds,
  durations and imperfections (blur, offset jitter and dropout, misidentification, gaps,
  cut-ins), some with editor ("manual") probes mixed in
- the real runs shuffled, subsampled and with probes repeated, since the fold depends on
  order and two probes can share a midpoint
- hand-edit shapes Python tolerates: int, bool, missing and null confidences, offsets that
  are empty, ints or bools, a missing timeskew, an empty result, int times
- names that differ by version tags, featuring credits, brackets, accents and marks, and by
  one or two letters around the clustering thresholds
- legacy (sequential) lists, which need ``--duration``

    uv run python scripts/parity_m2.py -o <scratch> [--fuzz 2000] [folder ...]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

from setlist_maker.adaptive import load_probes
from setlist_maker.boundary import BoundaryEngine, Probe

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # tests.boundary_oracle

from tests.boundary_oracle import SyntheticSet, SyntheticTrack, synthetic_identity  # noqa: E402

DEFAULT_FOLDERS = [
    Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Music/DJ Disarray 🎧",
    Path("/private/tmp/claude-501"),
    REPO / "crates/core/src/engine/fixtures",
]
BINARY = REPO / "target/release/hindsight"
KINDS = ["oracle", "oracle", "oracle", "shuffle", "values", "names", "legacy"]


# ---- Python's side -------------------------------------------------------------


def dump(path: Path, duration_hint: float | None) -> bytes:
    probes, duration = load_probes(path)
    duration = duration or duration_hint
    assert duration, f"{path} has no audio_duration"
    engine = BoundaryEngine(duration)
    lines = [_line(engine, 0, [])]
    for n, probe in enumerate(probes, 1):
        lines.append(_line(engine, n, engine.add_probe(probe)))
    return "".join(line + "\n" for line in lines).encode()


def _line(engine: BoundaryEngine, n: int, events: list) -> str:
    segments, drops = engine.segments()
    plan = engine.next_probe()
    value = {
        "n": n,
        "events": events,
        "segments": [
            {"start": s.start, "info": s.info, "confidence": s.confidence} for s in segments
        ],
        "drops": drops,
        "next": None
        if plan is None
        else {"t": plan.t, "window": plan.window, "purpose": plan.purpose},
        "stats": list(engine.boundary_stats()),
        "remaining": engine.estimated_probes_remaining(),
        "max_ratio": engine.max_ratio,
        "max_boundary_width": engine.max_boundary_width,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---- the corpus ------------------------------------------------------------------


def find_files(folders: list[Path], skip: Path) -> list[Path]:
    found: dict[str, Path] = {}
    for folder in folders:
        pattern = "*.json" if folder.name == "fixtures" else "*_progress.json"
        for path in sorted(folder.rglob(pattern)):
            if skip in path.parents:
                continue
            found.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), path)
    return list(found.values())


def _v2(duration: float, probes: list) -> dict:
    return {"version": 2, "audio_duration": duration, "probes": probes}


def _row(p: Probe) -> dict:
    return {
        "t": p.t,
        "window": p.window,
        "purpose": p.purpose,
        "result": p.result,
        "offsets": p.offsets,
    }


VARIANTS = [
    ("Daft Punk", "One More Time"),
    ("Daft Punk", "One More Time (Radio Edit)"),
    ("Daft Punk feat. Romanthony", "One More Time - Extended Mix"),
    ("Daft Punk", "One More Tim"),
    ("Daft Punks", "One More Time"),
    ("Daft Pank", "One Mere Time"),
    ("Boz Scaggs", "Lowdown"),
    ("Boz Scaggs", "Lowdown - Remastered 2007"),
    ("Boz Scaggs", "Lowdown [Live]"),
    ("Grant Green", "Sookie Sookie (Live)"),
    ("Us3", "Tukka Yoot's Riddim"),
    ("Björk", "Jóga"),
    ("Bjork", "Joga"),
    ("Sigur Rós", "Hoppípolla"),
    ("İbrahim", "Straße"),
    ("ibrahim", "strasse"),
    ("Ⅻ", "½ Time"),
    ("álive", "re-edit́"),
    ("Artist 1", "Track 1"),
    ("Artist 11", "Track 11"),
    ("", "Untitled"),
    ("Kraftwerk", ""),
    ("The J.B.'s", "These Are the JB's (Pt. 1 & 2)"),
    ("Herb Alpert", "Rise"),
    ("The Notorious B.I.G.", "Hypnotize"),
    ("Sly & The Family Stone", 'Africa Talks to You ("The Asphalt Jungle")'),
    ("Roy Ayers", "Pricilla's Theme {Edit} (with strings)"),
    ("Trio", "Da Da Da"),
    ("Trio", "Da Da Da Ich Lieb Dich Nicht Du Liebst Mich Nicht"),
    ("Nobody", "x" * 210 + "y" * 5),
    ("Nobody", "x" * 205 + "y" * 7),
    ("Tab\tCo", "Line\nBreak"),
]

# Two 204-character titles that score 0.95 as they are but 0.08 once difflib's "autojunk"
# drops the letters making up over 1% of the second: only autojunk keeps them apart.
JUNK_A = (
    "bebhhhgdbhaggaheabfaacagdgadhhdfddbeagbcebfcdeehgahdggcffbhbcgfhahaefccdaddgffhe"
    "agadgahbdghfgfffhadccbeabbahaedebcfebcceceehfhhbaegfgdebddgadagcachgdhdagehaecda"
    "ebbeecgecaadhcagdfbdgdhbgehafgeacdfcfgdebgfh"
)
JUNK_B = (
    "bebhhhgdbhaggahedbfaaaagdgadhhdfddheagbcebfgdeehgahdggcffbhbcgfhahaegccdaddgffhe"
    "agcdgahfdghfgfafhadccbeabbahaedebcfebcceceehfhhbaegfgdebedgadagcachgdhdagfgaecda"
    "ebbeecgecaadhcagdfbdgdhbgehafgeacdfcfgdebgfh"
)
VARIANTS += [("Long", JUNK_A), ("Long", JUNK_B)]

CONFIDENCES = [0.9, 0.6, 0.599, 0.2, 0.999, 1, 0, None, True, False, "missing"]


def _name(rng: random.Random) -> dict:
    artist, title = rng.choice(VARIANTS)
    result = {"title": title, "artist": artist}
    if rng.random() < 0.1:
        del result["artist"]
    conf = rng.choice(CONFIDENCES)
    if conf != "missing":
        result["confidence"] = conf
    return result


def _offsets(rng: random.Random, t: float, window: float) -> list | None:
    shape = rng.randrange(8)
    if shape == 0:
        return None
    if shape == 1:
        return []
    lead = max(0.0, (window - 10.0) / 2.0)
    start = rng.choice([t - 200.0, t - 30.0, t, t + 5.0, 0.0])
    out = []
    for _ in range(rng.randint(1, 4)):
        offset = t + lead - start + rng.choice([0.0, 0.5, 3.9, 4.1, 50.0])
        m: dict = {"offset": round(offset, rng.choice([0, 1, 3, 9])), "timeskew": 0.0}
        roll = rng.random()
        if roll < 0.1:
            m["offset"] = int(offset)
        elif roll < 0.15:
            m["offset"] = True
        elif roll < 0.2:
            del m["offset"]
        roll = rng.random()
        if roll < 0.1:
            del m["timeskew"]
        elif roll < 0.15:
            m["timeskew"] = None
        elif roll < 0.25:
            m["timeskew"] = rng.choice([0.02, -0.0201, 0.5, 1])
        out.append(m)
    return out


def _oracle(rng: random.Random) -> dict:
    duration = rng.choice([1.0, 45.0, 300.0, 1200.0, 3600.0, 3600.0, 7200.0, 14400.0])
    misid = rng.choice([0.0, 0.0, 0.12, 0.3])
    limit = 24 if misid else 60
    tracks, at = [], 0.0
    while at < duration and len(tracks) < limit:
        if rng.random() < 0.3:
            artist, title = rng.choice(VARIANTS)
        else:
            artist, title = synthetic_identity(len(tracks))
        cut_in = rng.uniform(0.0, 90.0) if rng.random() < 0.5 else 0.0
        conf = rng.choice([0.9, 0.9, 0.666, 0.3, 0.999])
        tracks.append(SyntheticTrack(artist, title, at, cut_in, conf))
        at += rng.choice([rng.uniform(10.0, 60.0), rng.uniform(120.0, 480.0)])
    gaps = []
    for _ in range(rng.choice([0, 0, 1, 3])):
        a = rng.uniform(0.0, duration)
        gaps.append((a, a + rng.uniform(5.0, 400.0)))
    oracle = SyntheticSet(
        duration=duration, tracks=tracks, gaps=tuple(gaps), seed=rng.randrange(1 << 30)
    )
    oracle.offset_jitter = rng.choice([0.0, 0.5, 2.0, 6.0])
    oracle.offset_dropout = rng.choice([0.0, 0.3, 1.0])
    oracle.edge_blur = rng.random() < 0.7
    oracle.misid_rate = misid
    engine = BoundaryEngine(duration)
    cap = rng.choice([5, 40, 400, 400, 400])
    rows = []
    while len(rows) < cap and (plan := engine.next_probe()) is not None:
        if rng.random() < 0.05:  # a click in the editor
            t = rng.uniform(0.0, max(0.0, duration - 12.0))
            result, offsets = oracle.answer(t, 12.0)
            probe = Probe(t, 12.0, "manual", result, offsets)
        else:
            probe = oracle.probe_for(plan)
        engine.add_probe(probe)
        rows.append(_row(probe))
    return _v2(duration, rows)


def _base(rng: random.Random, real: list[Path]) -> tuple[float, list[dict]]:
    if real and rng.random() < 0.6:
        probes, duration = load_probes(rng.choice(real))
        if duration:
            return duration, [_row(p) for p in probes]
    data = _oracle(rng)
    return data["audio_duration"], data["probes"]


def _shuffle(rng: random.Random, real: list[Path]) -> dict:
    duration, rows = _base(rng, real)
    if rows and rng.random() < 0.5:
        rows = rng.sample(rows, rng.randint(1, len(rows)))
    else:
        rng.shuffle(rows)
    for _ in range(rng.choice([0, 1, 5, 20])):
        if not rows:
            break
        twin = dict(rng.choice(rows))
        roll = rng.random()
        if roll < 0.4:  # the same midpoint, another answer
            twin["result"] = rng.choice(rows)["result"]
        elif roll < 0.7:  # the same midpoint through another window
            window = 42.0 - twin["window"] if twin["window"] in (12.0, 30.0) else 12.0
            twin["t"] = twin["t"] + (twin["window"] - window) / 2.0
            twin["window"] = window
            twin["result"] = rng.choice(rows)["result"]
        rows.insert(rng.randrange(len(rows) + 1), twin)
    return _v2(duration, rows)


def _values(rng: random.Random, real: list[Path]) -> dict:
    duration, rows = _base(rng, real)
    for row in rows:
        roll = rng.random()
        if roll < 0.05:
            row["result"] = {}
        elif roll < 0.15 and row["result"]:
            row["result"] = dict(row["result"])
            conf = rng.choice(CONFIDENCES)
            if conf == "missing":
                row["result"].pop("confidence", None)
            else:
                row["result"]["confidence"] = conf
        if rng.random() < 0.3:
            row["offsets"] = _offsets(rng, row["t"], row["window"])
        if rng.random() < 0.05:
            row["t"] = int(row["t"])
        if rng.random() < 0.03:
            row["purpose"] = rng.choice(["manual", "other"])
    return _v2(duration, rows)


def _names(rng: random.Random, real: list[Path]) -> dict:
    duration, rows = _base(rng, real)
    for row in rows:
        if row["result"] is not None or rng.random() < 0.3:
            row["result"] = _name(rng)
    return _v2(duration, rows)


def _legacy(rng: random.Random, real: list[Path]) -> tuple[list, float]:
    duration = rng.choice([600.0, 3600.0, 14400.0])
    rows, t = [], 0.0
    while t < duration and len(rows) < 300:
        info = None if rng.random() < 0.2 else _name(rng)
        rows.append([rng.choice([t, int(t)]), info])
        t += rng.choice([30.0, 30.0, 45.0])
    return rows, duration


def generate(kind: str, seed: int, real: list[Path], path: Path) -> float | None:
    """Writes one generated case; returns the duration replay needs, if the file lacks one."""
    rng = random.Random(seed)
    if kind == "legacy":
        rows, duration = _legacy(rng, real)
        path.write_text(json.dumps(rows))
        return duration
    make = {"oracle": lambda r, _: _oracle(r), "shuffle": _shuffle, "values": _values}
    data = make.get(kind, _names)(rng, real)
    path.write_text(json.dumps(data, indent=1))
    return None


# ---- comparing -------------------------------------------------------------------


def check(task: tuple, real: list[Path], cases: Path, binary: Path) -> tuple[str, int, str | None]:
    kind, ref = task
    if kind == "file":
        path, hint = Path(ref), None
        name = path.name
    else:
        name = f"{kind}{ref:05d}.json"
        path = cases / name
        hint = generate(kind, ref, real, path)
    try:
        ours = dump(path, hint)
    except Exception as e:  # a case Python itself can't read isn't a parity case
        return name, -1, f"python: {type(e).__name__}: {e}"
    cmd = [str(binary), "replay", str(path)]
    if hint is not None:
        cmd += ["--duration", repr(hint)]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        return name, ours.count(b"\n"), "hindsight: " + proc.stderr.decode(errors="replace")
    if proc.stdout == ours:
        return name, ours.count(b"\n"), None
    py_lines, rs_lines = ours.splitlines(), proc.stdout.splitlines()
    i = next(
        (k for k, (a, b) in enumerate(zip(py_lines, rs_lines)) if a != b),
        min(len(py_lines), len(rs_lines)),
    )
    (cases.parent / "diffs").mkdir(exist_ok=True)
    (cases.parent / "diffs" / f"{name}.py.jsonl").write_bytes(ours)
    (cases.parent / "diffs" / f"{name}.rs.jsonl").write_bytes(proc.stdout)
    return name, len(py_lines), f"line {i} differs (diffs/{name}.*.jsonl)"


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("folders", nargs="*", type=Path, default=DEFAULT_FOLDERS)
    ap.add_argument("-o", "--output-dir", type=Path, required=True)
    ap.add_argument("--fuzz", type=int, default=0, help="generated cases to add")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--binary", type=Path, help="a hindsight binary to check, instead of building one"
    )
    args = ap.parse_args()

    out = args.output_dir.resolve()
    if out.exists():
        shutil.rmtree(out)
    cases = out / "cases"
    cases.mkdir(parents=True)
    binary = args.binary or BINARY
    if not args.binary:
        subprocess.run(
            ["cargo", "build", "--release", "--quiet", "-p", "hindsight-cli"], cwd=REPO, check=True
        )

    real = find_files([f for f in args.folders if f.exists()], skip=out)
    tasks = [("file", str(p)) for p in real]
    tasks += [(KINDS[i % len(KINDS)], args.seed + i) for i in range(args.fuzz)]
    print(f"{len(real)} distinct progress files found, {args.fuzz} generated")

    results: dict[str, list] = {}
    with concurrent.futures.ProcessPoolExecutor() as pool:
        futures = [pool.submit(check, t, real, cases, binary) for t in tasks]
        for task, fut in zip(tasks, futures):
            name, lines, problem = fut.result()
            group = "real" if task[0] == "file" else task[0]
            results.setdefault(group, []).append((name, lines, problem))

    failed = 0
    for group, rows in results.items():
        bad = [r for r in rows if r[2] and r[1] >= 0]
        skipped = [r for r in rows if r[1] < 0]
        prefixes = sum(r[1] for r in rows if r[1] > 0)
        print(f"{group:8} {len(rows):5} files {prefixes:7} prefixes  {len(bad)} differ", end="")
        print(f", {len(skipped)} Python can't read" if skipped else "")
        for name, _, problem in (bad + skipped)[:5]:
            print(f"    {name}: {problem}")
        failed += len(bad)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
