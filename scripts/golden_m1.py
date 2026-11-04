"""M1's golden check: Python and Hindsight read and save the same sets, and must agree.

Every set found under the given folders is copied twice into an output folder (the
originals are only read). Python rewrites one copy, ``cargo run --example resave`` the
other: the tracklist (``load_tracklist`` + ``save_tracklist``), the episode tags
(``episode_tags`` + ``save_episode_tags``, each side with its own empty config folder) and
every progress file (``load_probes`` + ``save_progress_v2``). Each ``.md`` must be
byte-equal, and each ``.json`` must parse to the same values (Hindsight writes UTF-8 where
Python writes ``\\uXXXX``).

``--fuzz N`` adds N generated tracklists built from the shapes that have broken the format
before (#16, #44, #45): fences, escaped markers, CRs, partial rows, times inside titles, plus
hand-edited episode files and set names with and without a date.

    uv run python scripts/golden_m1.py -o <scratch> [--fuzz 2000] [folder ...]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

from setlist_maker.adaptive import load_probes, save_progress_v2
from setlist_maker.episode import episode_tags, save_episode_tags
from setlist_maker.tracklist import (
    CorrectionsDB,
    load_tracklist,
    parse_markdown_tracklist,
    save_tracklist,
)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FOLDERS = [
    Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Music/DJ Disarray 🎧",
    Path("/private/tmp/claude-501"),
]


Case = dict[str, bytes]  # file name -> contents; the tracklist is the one ending _tracklist.md


def _distinct(files: list[Case]) -> list[Case]:
    seen: set[str] = set()
    out = []
    for case in files:
        digest = hashlib.sha256(repr(sorted(case.items())).encode()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            out.append(case)
    return out


def find_sets(folders: list[Path], skip: Path) -> tuple[list[Case], list[Case]]:
    """Distinct sets (tracklist, sidecar, episode file) and progress files under the
    folders, outside ``skip``."""
    sets: list[Case] = []
    progress: list[Case] = []
    for folder in folders:
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.*")):
            if path.resolve().is_relative_to(skip) or not path.is_file():
                continue
            if path.name.endswith("_tracklist.md"):
                case = {path.name: path.read_bytes()}
                stem = path.name[: -len("_tracklist.md")]
                for sibling in (path.with_suffix(".json"), path.with_name(f"{stem}_episode.json")):
                    if sibling.exists():
                        case[sibling.name] = sibling.read_bytes()
                sets.append(case)
            elif "_progress" in path.name and path.suffix == ".json":
                progress.append({path.name: path.read_bytes()})
    return _distinct(sets), _distinct(progress)


# --- generated cases ---------------------------------------------------------------------

PIECES = [
    "Daft Punk",
    "Altın Gün",
    "Кино",
    "Björk",
    "(1:23) Reprise",
    "(4:33)",
    "Song (Remastered 2009)",
    "A - B",
    "**bold**",
    "*Unknown artist*",
    "*Unidentified*",
    "Un*known",
    "1. **X** - Y (0:00)",
    "  padded  ",
    "",
    "   ",
    "x (9:99:99)",
    "\\<!-- /summary -->",
    "tab\there",
]
MARKERS = ["<!-- summary -->", "<!-- /summary -->"]


def _time(rng: random.Random) -> str:
    s = rng.choice([0, 59, 60, 3599, 3600, 5445, 36000, rng.randrange(0, 20000)])
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _track_line(rng: random.Random, n: int) -> str:
    a, t = rng.choice(PIECES), rng.choice(PIECES)
    shape = rng.randrange(6)
    if shape == 0:
        body = f"**{a}** - {t}"
    elif shape == 1:
        body = f"*Unknown artist* - {t}"
    elif shape == 2:
        body = f"**{a}**"
    elif shape == 3:
        body = "*Unidentified*"
    elif shape == 4:
        body = f"**{a}** -  "
    else:
        body = f"**** - {t}"
    tail = rng.choice(["", "", " [live]", "  "])
    return f"{n}. {body} ({_time(rng)}){tail}"


def _prose(rng: random.Random) -> str:
    return rng.choice(
        [
            "A late-night set.",
            "*Generated on* a rainy Tuesday.",
            "# Tracklist: fake.mp3",
            rng.choice(MARKERS),
            "\\" + rng.choice(MARKERS),
            "  " + rng.choice(MARKERS) + "  ",
            "<!-- summary --> trailing",
            _track_line(rng, 1),
            "",
        ]
    )


def generate(rng: random.Random) -> str:
    lines = []
    if rng.random() < 0.95:
        lines.append(f"# Tracklist: {rng.choice(['set.mp3', 'Altın.mp3', ''])}")
        lines.append("")
    if rng.random() < 0.95:
        lines.append(f"*Generated on {rng.choice(['2026-01-31 20:00', 'x', '2026-11-03'])}*")
        lines.append("")
    fenced = rng.random()
    if fenced < 0.5:
        lines.append(MARKERS[0])
        lines.extend(_prose(rng) for _ in range(rng.randrange(0, 4)))
        if rng.random() < 0.85:
            lines.append(MARKERS[1])
        lines.append("")
    elif fenced < 0.75:
        lines.extend(_prose(rng) for _ in range(rng.randrange(0, 3)))
        lines.append("")
    for n in range(1, rng.randrange(0, 8) + 1):
        lines.append(_track_line(rng, n) if rng.random() < 0.85 else _prose(rng))
    if rng.random() < 0.1:
        lines.append(MARKERS[1])
    sep = rng.choice(["\n", "\n", "\r\n", "\r"])
    return sep.join(lines) + rng.choice(["\n", "", "\n\n"])


SET_NAMES = ["2026-09-23-Keys-Lounge", "2026-01-05", "Keys", "2026-13-40-x", "2028-02-29b", ""]
EPISODES = [
    None,
    None,
    b'{"title": "  Keys \\n Lounge ", "artist": "DJ Disarray"}',
    b'{"title": 5}',
    b'{"artist": ""}',
    b"not json",
    b"[1, 2]",
    b'{"artist": "' + "\u0131".encode() * 300 + b'"}',
]


def generate_set(rng: random.Random, i: int) -> Case:
    name = f"{rng.choice(SET_NAMES)}{i}_tracklist.md"
    case = {name: generate(rng).encode()}
    episode = rng.choice(EPISODES)
    if episode is not None:
        case[name.replace("_tracklist.md", "_episode.json")] = episode
    return case


def generate_progress(rng: random.Random, i: int, results: list) -> Case:
    """A legacy list or a v2 file made from real answers, with awkward floats."""
    floats = [0.1 + 0.2, 1e-7, 1e16, 108.76374218699999, 7200.252, 0.0, 5.0]
    picked = rng.sample(results, k=min(len(results), rng.randrange(0, 12)))
    if rng.random() < 0.5:
        rows = [[rng.choice([30 * n, rng.choice(floats)]), r] for n, r in enumerate(picked)]
        data: object = rows
    else:
        probes = [
            {
                "t": rng.choice(floats),
                "window": rng.choice([30.0, 12.0, 30]),
                "purpose": rng.choice(["coverage", "refine", "manual"]),
                "result": r,
                "offsets": rng.choice(
                    [None, [], [{"offset": rng.choice(floats), "timeskew": 1e-5}]]
                ),
            }
            for r in picked
        ]
        data = {"version": 2, "audio_duration": rng.choice(floats + [None]), "probes": probes}
    return {f"gen{i}_progress.json": json.dumps(data, indent=rng.choice([None, 2])).encode()}


NAMES = ["İstanbul", "ΟΔΟΣ", "Straße", "STRASSE", "ǅemal", "  Padded  ", "Ⅻ", "Björk", "ﬁn"]


def generate_corrections(rng: random.Random, path: Path) -> None:
    """Corrections Python itself writes, keyed by its own lowercasing of awkward names."""
    db = CorrectionsDB(db_path=path)
    for _ in range(200):
        db.add_correction(rng.choice(NAMES), rng.choice(NAMES), rng.choice(NAMES), "T")
    db.save()


# --- running and comparing ----------------------------------------------------------------

_STAMP = re.compile(r"^\*Generated on [^\n]*\*$", re.MULTILINE)


def _unstamped(md: bytes, original: bytes) -> bytes:
    """The markdown with its date blanked when the input had none, so both writers stamped
    the current time and a minute ticking over between them isn't a difference."""
    with contextlib.redirect_stdout(io.StringIO()):
        dated = parse_markdown_tracklist(original.decode().replace("\r\n", "\n")).generated_on
    if dated:
        return md
    return _STAMP.sub("*Generated on <now>*", md.decode(), count=1).encode()


def _copy(cases: list[Case], root: Path) -> list[Path]:
    """Writes each case into its own folder; returns each one's main file."""
    mains = []
    for i, case in enumerate(cases):
        d = root / f"{i:05d}"
        d.mkdir(parents=True)
        for name, data in case.items():
            (d / name).write_bytes(data)
        mains.append(d / next(n for n in case if not n.endswith("_episode.json")))
    return mains


def _same_json(a: Path, b: Path) -> bool:
    if not a.exists() or not b.exists():
        return a.exists() == b.exists()
    return json.loads(a.read_text()) == json.loads(b.read_text())


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("-o", "--out", type=Path, required=True, help="scratch folder (emptied)")
    ap.add_argument("--fuzz", type=int, default=0, help="generated cases to add")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("folders", nargs="*", type=Path, default=DEFAULT_FOLDERS)
    args = ap.parse_args()

    logging.disable(logging.WARNING)  # "ignoring unreadable ..." for the junk episode files
    sets, progress = find_sets(args.folders, args.out.resolve())
    real = len(sets)
    rng = random.Random(args.seed)
    sets += [generate_set(rng, i) for i in range(args.fuzz)]
    results = [None] + [
        probe["result"]
        for case in progress
        for probe in json.loads(next(iter(case.values()))).get("probes", [])
    ]
    real_progress = len(progress)
    progress += [generate_progress(rng, i, results) for i in range(args.fuzz // 50)]

    if args.out.exists():
        shutil.rmtree(args.out)
    corrections = [Path.home() / ".config/setlist-maker/corrections.json"]
    corrections = [p.read_bytes() for p in corrections if p.exists()]
    if args.fuzz:
        made = args.out / "generated_corrections.json"
        generate_corrections(rng, made)
        corrections.append(made.read_bytes())
    side = {}
    for name in ("py", "rs"):
        side[name] = (
            _copy(sets, args.out / name / "sets"),
            _copy(progress, args.out / name / "progress"),
            args.out / name / "config",
            _copy([{"corrections.json": c} for c in corrections], args.out / name / "corrections"),
        )

    py_sets, py_progress, py_config, py_corrections = side["py"]
    os.environ["XDG_CONFIG_HOME"] = str(py_config)
    with contextlib.redirect_stdout(io.StringIO()):  # the parse warnings
        for p in py_sets:
            save_tracklist(load_tracklist(p), p)
            tags = episode_tags(p)
            save_episode_tags(p, tags["title"], tags["artist"])
    for p in py_progress:
        probes, duration = load_probes(p)
        save_progress_v2(duration, probes, p)  # pyright: ignore[reportArgumentType] # None stays null
    for p in py_corrections:
        CorrectionsDB(db_path=p).save()

    rs_sets, rs_progress, rs_config, rs_corrections = side["rs"]
    resave = ["cargo", "run", "-q", "-p", "hindsight-core", "--example", "resave", "--"]
    env = {**os.environ, "XDG_CONFIG_HOME": str(rs_config)}
    for kind, paths in (
        ("tracklist", rs_sets),
        ("episode", rs_sets),
        ("progress", rs_progress),
        ("corrections", rs_corrections),
    ):
        run = subprocess.run(
            resave + [kind] + [str(p) for p in paths], cwd=REPO, env=env, capture_output=True
        )
        if run.returncode != 0:  # parse warnings alone exit 0
            print(f"resave {kind} failed:\n{run.stderr.decode()[-2000:]}")
            return 1

    failures = 0

    def report(py: Path, rs: Path, problems: list[str]) -> None:
        nonlocal failures
        if problems:
            failures += 1
            if failures <= 10:
                print(f"{py.parent.name} {py.name}: {', '.join(problems)}")
                print(f"  diff -u {py} {rs}")

    for case, py, rs in zip(sets, py_sets, rs_sets):
        original = case[py.name]
        problems = []
        if _unstamped(py.read_bytes(), original) != _unstamped(rs.read_bytes(), original):
            problems.append("markdown differs")
        if not _same_json(py.with_suffix(".json"), rs.with_suffix(".json")):
            problems.append("sidecar differs")
        episode = py.name.replace("_tracklist.md", "_episode.json")
        if not _same_json(py.with_name(episode), rs.with_name(episode)):
            problems.append("episode differs")
        report(py, rs, problems)
    for py, rs in zip(py_progress, rs_progress):
        report(py, rs, [] if _same_json(py, rs) else ["progress differs"])
    for py, rs in zip(py_corrections, rs_corrections):
        report(py, rs, [] if _same_json(py, rs) else ["corrections differ"])
    config = Path("hindsight/config.json"), Path("setlist-maker/config.json")
    report(
        py_config,
        rs_config,
        []
        if _same_json(py_config / config[1], rs_config / config[0])
        else ["remembered artist differs"],
    )

    print(
        f"{len(sets)} sets ({real} found on disk, {args.fuzz} generated) and "
        f"{len(progress)} progress files ({real_progress} found on disk) and "
        f"{len(corrections)} corrections files: {failures} differ"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
