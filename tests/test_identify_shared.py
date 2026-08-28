"""Shared identify helpers used by both the sequential and adaptive drivers."""

import json

from setlist_maker.identify import finalize_outputs, results_to_tracklist

RAW = [
    (0, {"artist": "A", "title": "One", "confidence": 0.9}),
    (180, {"artist": "B", "title": "Two", "confidence": 0.9}),
    (400, {"artist": "A", "title": "One", "confidence": 0.9}),
]


def test_deduplicate_false_keeps_single_sample_tracks():
    tracklist = results_to_tracklist(RAW, "set.mp3", deduplicate=False)
    # Every entry survives: no singleton filter, no smoothing, no collapse.
    assert [t.title for t in tracklist.tracks] == ["One", "Two", "One"]


def test_deduplicate_false_still_applies_corrections():
    class FakeDB:
        def get_correction(self, artist, title):
            return ("A!", "One!") if title == "One" else None

    tracklist = results_to_tracklist(RAW, "set.mp3", FakeDB(), deduplicate=False)
    assert tracklist.tracks[0].artist == "A!"
    assert tracklist.tracks[0].original_title == "One"


def test_finalize_outputs_writes_markdown_and_sidecar(tmp_path):
    tracklist = results_to_tracklist(RAW, "set.mp3", deduplicate=False)
    out = tmp_path / "set_tracklist.md"
    finalize_outputs(tracklist, out, summary=False)
    assert out.exists()
    sidecar = json.loads((tmp_path / "set_tracklist.json").read_text())
    assert isinstance(sidecar, list)  # the bare-list contract (see CLAUDE.md)
