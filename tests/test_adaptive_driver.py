"""End-to-end adaptive driver against the synthetic oracle (no network)."""

import asyncio
import json
import signal

import setlist_maker.adaptive as adaptive
from setlist_maker.adaptive import (
    EventLog,
    _sigint_flag,
    format_probe_line,
    process_single_file_adaptive,
)
from tests.boundary_oracle import SyntheticSet, SyntheticTrack


class FakeAudio:
    def __init__(self, seconds):
        self._ms = int(seconds * 1000)

    def __len__(self):
        return self._ms


def _wire(monkeypatch, oracle):
    monkeypatch.setattr(
        adaptive, "load_audio", lambda p, allow_partial=False: FakeAudio(oracle.duration)
    )
    monkeypatch.setattr(adaptive, "extract_window", lambda a, t, w: (t, w))

    calls = {"n": 0}

    async def fake_identify(shazam, segment, temp_dir, include_offsets=False, on_backoff=None):
        calls["n"] += 1
        result, offsets = oracle.answer(*segment)
        if result and include_offsets:
            result = {**result, "offsets": offsets or []}
        return result

    monkeypatch.setattr(adaptive, "identify_sample_with_retry", fake_identify)
    return calls


def _oracle():
    return SyntheticSet(
        duration=900.0,
        tracks=[
            SyntheticTrack("A", "Alpha", 0.0),
            SyntheticTrack("B", "Beta", 300.0),
            SyntheticTrack("C", "Gamma", 610.0),
        ],
        seed=1,
    )


def test_driver_end_to_end(tmp_path, monkeypatch):
    oracle = _oracle()
    _wire(monkeypatch, oracle)
    result = asyncio.run(
        process_single_file_adaptive(
            audio_path=tmp_path / "set.mp3",
            output_dir=None,
            delay_seconds=0,
            summary=False,
        )
    )
    assert result is not None
    tracklist, output_path = result
    titles = [t.title for t in tracklist.tracks]
    assert titles == ["Alpha", "Beta", "Gamma"]
    for true, got in zip([300.0, 610.0], [t.timestamp for t in tracklist.tracks[1:]]):
        assert abs(true - got) <= 5.0
    assert output_path.exists()
    assert (tmp_path / "set_tracklist.json").exists()
    assert (tmp_path / "set_progress.json").exists()
    events = [json.loads(line) for line in (tmp_path / "set_events.jsonl").read_text().splitlines()]
    assert events[-1]["type"] == "finalized"
    assert any(e["type"] == "probe_result" for e in events)


def test_driver_resume_is_replay(tmp_path, monkeypatch):
    oracle = _oracle()
    calls = _wire(monkeypatch, oracle)
    asyncio.run(
        process_single_file_adaptive(
            audio_path=tmp_path / "set.mp3", output_dir=None, delay_seconds=0, summary=False
        )
    )
    first_run = calls["n"]
    asyncio.run(
        process_single_file_adaptive(
            audio_path=tmp_path / "set.mp3", output_dir=None, delay_seconds=0, summary=False
        )
    )
    assert calls["n"] == first_run  # converged replay asks Shazam nothing


def test_budget_zero_stops_immediately_but_finalizes(tmp_path, monkeypatch):
    oracle = _oracle()
    calls = _wire(monkeypatch, oracle)
    result = asyncio.run(
        process_single_file_adaptive(
            audio_path=tmp_path / "set.mp3",
            output_dir=None,
            delay_seconds=0,
            summary=False,
            budget_seconds=0,
        )
    )
    assert result is not None
    assert calls["n"] == 0
    events = (tmp_path / "set_events.jsonl").read_text()
    assert "budget_exhausted" in events and "finalized" in events


def test_sigint_flag_sets_then_raises():
    with _sigint_flag() as flag:
        assert flag.stop is False
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert flag.stop is True
        try:
            handler(signal.SIGINT, None)
            raised = False
        except KeyboardInterrupt:
            raised = True
        assert raised


def test_event_log_appends(tmp_path):
    path = tmp_path / "e.jsonl"
    with EventLog(path) as log:
        log.write({"type": "a"})
    with EventLog(path) as log:
        log.write({"type": "b"})
    types = [json.loads(line)["type"] for line in path.read_text().splitlines()]
    assert types == ["a", "b"]


def test_format_probe_line_shapes():
    line = format_probe_line(310.0, "refine", {"artist": "B", "title": "Beta", "confidence": 0.9})
    assert "5:10" in line and "Beta" in line
    miss = format_probe_line(310.0, "coverage", None)
    assert "not identified" in miss
