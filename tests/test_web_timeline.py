"""Tests for the timeline's pure derivations (setlist_maker/web_timeline.js).

The page has no JS harness, but these functions decide which tracks the editor
calls broken and what one click does about it, so they are run under Node
rather than asserted as substrings. Skipped where Node is not installed.
"""

import json
import shutil
import subprocess
from importlib.resources import files

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = str(files("setlist_maker") / "web_timeline.js")


def js(expr: str, **data):
    """Evaluate ``expr`` with the module bound to ``T`` and ``data`` as globals."""
    prelude = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in data.items())
    program = (
        f"const T = require({json.dumps(SCRIPT)});\n{prelude}"
        f"process.stdout.write(JSON.stringify({expr}));"
    )
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def track(ts, artist="", title="", rejected=False):
    return {"timestamp": ts, "artist": artist, "title": title, "rejected": rejected}


def probe(t, artist=None, title=None, window=30.0, purpose="coverage", confidence=0.66):
    return {
        "t": t,
        "window": window,
        "purpose": purpose,
        "artist": artist,
        "title": title,
        "confidence": confidence,
    }


# The opening of the real 2026-09-23 set: Computer Love is broken into three
# rows by three short blips Shazam heard inside it.
KRAFTWERK = [
    track(1036, "Kraftwerk", "Computer Love (2009 Remaster)"),
    track(1253, "David Guetta", "Titanium (Alesso Remix) [feat. Sia]"),
    track(1264, "Ali Kuru", "Rhythm Is a Dancer"),
    track(1267, "Kraftwerk", "Computer Love (2009 Remaster)"),
    track(1349, "Jam & Spoon", "Right in the Night (feat. Plavka) [Morttagua Extended Remix]"),
    track(1357, "Kraftwerk", "Computer Love (2009 Remaster)"),
    track(1475, "Talking Heads", "Moon Rocks"),
]


def test_clean_title_drops_reissue_tags_and_nothing_else():
    got = js(
        "titles.map(T.cleanTitle)",
        titles=[
            "Computer Love (2009 Remaster)",
            "Danger (Remastered)",
            "Tress-Cun-Deo-La (Remastered 2025)",
            "Mirror in the Bathroom - 2012 Remaster",
            "Things Fall Apart (Vocal)",
            "Anna - Lassmichreinlassmichraus (Maxi Version)",
        ],
    )
    assert got == [
        "Computer Love",
        "Danger",
        "Tress-Cun-Deo-La",
        "Mirror in the Bathroom",
        "Things Fall Apart (Vocal)",
        "Anna - Lassmichreinlassmichraus (Maxi Version)",
    ]


def test_identity_ignores_metadata_drift_but_not_a_different_song():
    same, different = js(
        "[T.identity('Kraftwerk', 'Computer Love (2009 Remaster)') === "
        "T.identity('kraftwerk', 'Computer Love'), "
        "T.identity('Trio', 'Da Da Da') === T.identity('Trio', 'Anna')]"
    )
    assert same is True
    assert different is False


def test_join_attaches_each_probe_by_its_midpoint():
    tracks = [track(0, "A", "a"), track(100, "B", "b")]
    # 80 + 30/2 = 95 is still A's; 90 + 15 = 105 is B's.
    got = js(
        "T.joinProbes(tracks, probes, 200).map(l => l.map(p => p.t))",
        tracks=tracks,
        probes=[probe(90, "B", "b"), probe(80, "A", "a"), probe(150, "B", "b")],
    )
    assert got == [[80], [90, 150]]


def test_blips_inside_one_track_are_one_merge():
    issues = js("T.findIssues(tracks, [], 1600)", tracks=KRAFTWERK)
    merges = [i for i in issues if i["kind"] == "interrupted"]
    assert len(merges) == 1
    assert merges[0]["index"] == 0
    # Every row after the first Computer Love, up to Moon Rocks, folds into it.
    assert merges[0]["fix"]["reject"] == [1, 2, 3, 4, 5]
    assert "3 short blips" in merges[0]["title"]
    # The blips are explained by the merge, so they are not also "too short".
    assert not [i for i in issues if i["kind"] == "short"]


def test_a_merge_once_applied_is_no_longer_found():
    merged = [dict(t, rejected=i in (1, 2, 3, 4, 5)) for i, t in enumerate(KRAFTWERK)]
    issues = js("T.findIssues(tracks, [], 1600)", tracks=merged)
    assert not [i for i in issues if i["kind"] in ("interrupted", "short")]


def test_a_long_different_track_is_not_a_blip():
    tracks = [track(0, "A", "a"), track(200, "B", "b"), track(400, "A", "a")]
    issues = js("T.findIssues(tracks, [], 600)", tracks=tracks)
    assert not [i for i in issues if i["kind"] == "interrupted"]


def test_short_track_between_different_tracks_offers_merge_up():
    tracks = [track(0, "A", "a"), track(200, "X", "x"), track(220, "B", "b")]
    issues = js("T.findIssues(tracks, [], 600)", tracks=tracks)
    short = [i for i in issues if i["kind"] == "short"]
    assert [s["index"] for s in short] == [1]
    assert short[0]["fix"]["reject"] == [1]


def test_variants_ignore_names_that_belong_to_the_neighbours():
    tracks = [track(0, "A", "a"), track(300, "Trio", "Da Da Da (Ich)"), track(600, "C", "c")]
    probes = [
        probe(290, "A", "a"),  # boundary evidence for the previous track
        probe(350, "Trio", "Da Da Da (Ich)"),
        probe(420, "Trio", "Da Da Da (I Don't Love You)"),
        probe(480, "Trio", "Da Da Da (Ich)"),
        probe(570, "C", "c"),  # and for the next one
    ]
    issues = js("T.findIssues(tracks, probes, 900)", tracks=tracks, probes=probes)
    variants = [i for i in issues if i["kind"] == "variants"]
    assert [v["index"] for v in variants] == [1]
    assert variants[0]["title"] == "Heard under 2 names"


def test_remaster_tags_are_one_batched_fix():
    tracks = [
        track(0, "Kraftwerk", "Pocket Calculator (2009 Remaster)"),
        track(300, "Pylon", "Danger (Remastered)"),
        track(600, "Squeeze", "In Quintessence"),
    ]
    issues = js("T.findIssues(tracks, [], 900)", tracks=tracks)
    tidy = [i for i in issues if i["kind"] == "remaster"]
    assert len(tidy) == 1
    assert tidy[0]["fix"]["retitle"] == [
        {"index": 0, "title": "Pocket Calculator"},
        {"index": 1, "title": "Danger"},
    ]


def test_unheard_stretches_are_reported():
    probes = [probe(0), probe(90), probe(1000)]
    assert js("T.unheard(probes, 1400)", probes=probes) == [[120, 1000], [1030, 1400]]


def test_rejected_tracks_hand_their_span_to_the_previous_kept_track():
    tracks = [track(0, "A", "a"), track(100, "X", "x", rejected=True), track(300, "B", "b")]
    got = js("T.keptWindows(tracks, 500)", tracks=tracks)
    assert got == [{"index": 0, "start": 0, "end": 300}, {"index": 2, "start": 300, "end": 500}]


def test_a_lone_stray_from_another_artist_is_not_a_second_name():
    tracks = [track(0, "A", "a"), track(300, "Kraftwerk", "Computer Love"), track(600, "C", "c")]
    probes = [
        probe(350, "Kraftwerk", "Computer Love"),
        probe(420, "David Guetta", "All Night"),
        probe(480, "Kraftwerk", "Computer Love"),
    ]
    issues = js("T.findIssues(tracks, probes, 900)", tracks=tracks, probes=probes)
    assert not [i for i in issues if i["kind"] == "variants"]
    # ...but twice is a pattern worth a look.
    probes.append(probe(500, "David Guetta", "All Night"))
    issues = js("T.findIssues(tracks, probes, 900)", tracks=tracks, probes=probes)
    assert [i["index"] for i in issues if i["kind"] == "variants"] == [1]


def test_a_tidied_title_is_not_a_rival_of_shazams_spelling():
    tracks = [track(0, "A", "a"), track(300, "Kraftwerk", "Computer Love"), track(600, "C", "c")]
    probes = [probe(350 + 60 * k, "Kraftwerk", "Computer Love (2009 Remaster)") for k in range(3)]
    issues = js("T.findIssues(tracks, probes, 900)", tracks=tracks, probes=probes)
    assert not [i for i in issues if i["kind"] == "variants"]


def test_a_merge_pools_the_merged_rows_probes_into_the_kept_track():
    tracks = [track(0, "A", "a"), track(100, "X", "x", rejected=True), track(300, "B", "b")]
    got = js(
        "T.joinProbes(tracks, probes, 500).map(l => l.map(p => p.t))",
        tracks=tracks,
        probes=[probe(20, "A", "a"), probe(150, "X", "x"), probe(350, "B", "b")],
    )
    # A owns 0-300 now; the merged row still lists what was heard in its old span.
    assert got == [[20, 150], [150], [350]]


def test_what_was_heard_inside_a_merged_stretch_is_not_a_rival():
    # The real set: Parov Stelar was heard twice at 0.99 inside the blips the
    # merge folded into Computer Love. Merging that stretch settled it, so the
    # merge must not immediately raise a new "Heard under 2 names".
    merged = [dict(t, rejected=i in (1, 2, 3, 4, 5)) for i, t in enumerate(KRAFTWERK)]
    probes = [
        probe(1100, "Kraftwerk", "Computer Love (2009 Remaster)"),
        probe(1245, "Parov Stelar", "All Night"),
        probe(1251, "Parov Stelar", "All Night", window=12.0),
        probe(1250, "David Guetta", "Titanium (Alesso Remix) [feat. Sia]", window=12.0),
        probe(1300, "Kraftwerk", "Computer Love (2009 Remaster)"),
    ]
    issues = js("T.findIssues(tracks, probes, 1600)", tracks=merged, probes=probes)
    assert not [i for i in issues if i["kind"] == "variants"]
    # ...whereas heard in Computer Love's own span -- including the span of a
    # merged repeat of it, which was never in dispute -- it is still a question.
    probes += [probe(1100, "Parov Stelar", "All Night"), probe(1400, "Parov Stelar", "All Night")]
    issues = js("T.findIssues(tracks, probes, 1600)", tracks=merged, probes=probes)
    assert [i["index"] for i in issues if i["kind"] == "variants"] == [0]
