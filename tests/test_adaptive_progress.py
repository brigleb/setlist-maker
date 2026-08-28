"""Progress v2: fold-ready persistence with legacy sequential conversion."""

import json

from setlist_maker.adaptive import load_probes, save_progress_v2
from setlist_maker.boundary import Probe

INFO = {"artist": "X", "title": "A", "confidence": 0.9}


def test_v2_round_trip(tmp_path):
    path = tmp_path / "p.json"
    probes = [
        Probe(
            t=100.0,
            window=30.0,
            purpose="coverage",
            result=INFO,
            offsets=[{"offset": 10.0, "timeskew": 0.0}],
        ),
        Probe(t=200.0, window=12.0, purpose="refine", result=None, offsets=None),
    ]
    save_progress_v2(3600.0, probes, path)
    loaded, duration = load_probes(path)
    assert duration == 3600.0
    assert loaded == probes


def test_legacy_list_converts_to_coverage_probes(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps([[0, INFO], [30, None], [60, INFO]]))
    loaded, duration = load_probes(path)
    assert duration is None
    assert [p.t for p in loaded] == [0.0, 30.0, 60.0]
    assert all(p.window == 30.0 and p.purpose == "coverage" for p in loaded)
    assert loaded[1].result is None


def test_missing_file_loads_empty(tmp_path):
    loaded, duration = load_probes(tmp_path / "absent.json")
    assert loaded == [] and duration is None


def test_sequential_refuses_v2_progress(tmp_path, capsys):
    import asyncio

    from setlist_maker.identify import process_single_file

    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"")
    progress = tmp_path / "set_progress.json"
    progress.write_text(json.dumps({"version": 2, "audio_duration": 60.0, "probes": []}))

    import setlist_maker.identify as identify_mod

    class FakeAudio:
        def __len__(self):
            return 60_000

        def __getitem__(self, sl):  # slice_audio runs before the progress check
            return self

    original = identify_mod.load_audio
    identify_mod.load_audio = lambda p, allow_partial=False: FakeAudio()
    try:
        result = asyncio.run(process_single_file(audio, None, delay_seconds=0, resume=True))
    finally:
        identify_mod.load_audio = original
    assert result is None
    assert "adaptive" in capsys.readouterr().out
