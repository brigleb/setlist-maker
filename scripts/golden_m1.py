"""M1's golden check: Python and Hindsight read and save the same sets, and must agree.

Every set found under the given folders is copied twice into an output folder (the
originals are only read). Python's ``load_tracklist`` + ``save_tracklist`` rewrites one copy,
``cargo run --example resave`` the other. Each ``.md`` must be byte-equal, and each ``.json``
must parse to the same values (Hindsight writes UTF-8 where Python writes ``\\uXXXX``).

``--fuzz N`` adds N generated tracklists built from the shapes that have broken the format
before (#16, #44, #45): fences, escaped markers, CRs, partial rows, times inside titles.

    uv run python scripts/golden_m1.py -o <scratch> [--fuzz 2000] [folder ...]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

from setlist_maker.tracklist import load_tracklist, parse_markdown_tracklist, save_tracklist

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FOLDERS = [
    Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Music/DJ Disarray 🎧",
    Path("/private/tmp/claude-501"),
]


def find_sets(folders: list[Path], skip: Path) -> list[tuple[str, bytes, bytes | None]]:
    """Distinct (name, markdown, sidecar) triples under the folders, outside ``skip``."""
    seen: set[str] = set()
    found = []
    for folder in folders:
        if not folder.exists():
            continue
        for md in sorted(folder.rglob("*_tracklist.md")):
            if md.resolve().is_relative_to(skip):
                continue
            sidecar = md.with_suffix(".json")
            md_bytes = md.read_bytes()
            json_bytes = sidecar.read_bytes() if sidecar.exists() else None
            digest = hashlib.sha256(md_bytes + b"\0" + (json_bytes or b"")).hexdigest()
            if digest not in seen:
                seen.add(digest)
                found.append((md.name, md_bytes, json_bytes))
    return found


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", type=Path, required=True, help="scratch folder (emptied)")
    ap.add_argument("--fuzz", type=int, default=0, help="generated cases to add")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("folders", nargs="*", type=Path, default=DEFAULT_FOLDERS)
    args = ap.parse_args()

    cases = find_sets(args.folders, args.out.resolve())
    real = len(cases)
    rng = random.Random(args.seed)
    for i in range(args.fuzz):
        cases.append((f"fuzz{i}_tracklist.md", generate(rng).encode(), None))

    if args.out.exists():
        shutil.rmtree(args.out)
    py_paths, rs_paths = [], []
    for i, (name, md, sidecar) in enumerate(cases):
        for side, paths in (("py", py_paths), ("rs", rs_paths)):
            d = args.out / side / f"{i:05d}"
            d.mkdir(parents=True)
            (d / name).write_bytes(md)
            if sidecar is not None:
                (d / name).with_suffix(".json").write_bytes(sidecar)
            paths.append(d / name)

    with contextlib.redirect_stdout(io.StringIO()):  # the parse warnings
        for p in py_paths:
            save_tracklist(load_tracklist(p), p)
    subprocess.run(
        ["cargo", "run", "-q", "-p", "hindsight-core", "--example", "resave", "--", "tracklist"]
        + [str(p) for p in rs_paths],
        cwd=REPO,
        check=True,
        stderr=subprocess.DEVNULL,
    )

    failures = 0
    for (_, original, _), py, rs in zip(cases, py_paths, rs_paths):
        problems = []
        if _unstamped(py.read_bytes(), original) != _unstamped(rs.read_bytes(), original):
            problems.append("markdown differs")
        if json.loads(py.with_suffix(".json").read_text()) != json.loads(
            rs.with_suffix(".json").read_text()
        ):
            problems.append("sidecar differs")
        if problems:
            failures += 1
            if failures <= 10:
                print(f"{py.parent.name} {py.name}: {', '.join(problems)}")
                print(f"  diff -u {py} {rs}")
    print(f"{len(cases)} sets ({real} found on disk, {args.fuzz} generated): {failures} differ")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
