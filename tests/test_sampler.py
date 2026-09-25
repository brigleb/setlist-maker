"""Tests for the editor's click-to-sample (setlist_maker.sampler) and its endpoint."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from setlist_maker.adaptive import append_probe, load_probes
from setlist_maker.boundary import Probe
from setlist_maker.sampler import ShazamSampler, sample_window_start


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def sampler(tmp_path, answer=None, clock=None, **kw):
    clock = clock or FakeClock()
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"")
    seen = []

    def slicer(path, start, window):
        seen.append((start, window))
        return "segment"

    s = ShazamSampler(
        audio,
        recognize=lambda seg: dict(answer) if answer else None,
        slicer=slicer,
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )
    return s, seen, clock


def test_window_is_centred_on_the_moment_asked_about():
    # shazamio fingerprints the middle 10s of what it is handed.
    assert sample_window_start(100.0) == 85.0
    assert sample_window_start(5.0) == 0.0


def test_sample_returns_a_manual_probe_and_strips_offsets(tmp_path):
    answer = {"artist": "Trio", "title": "Da Da Da", "offsets": [{"offset": 12.0}]}
    s, seen, _ = sampler(tmp_path, answer=answer)
    probe = s(100.0)
    assert seen == [(85.0, 30.0)]
    assert probe.purpose == "manual"
    assert probe.t == 85.0 and probe.window == 30.0
    assert probe.result == {"artist": "Trio", "title": "Da Da Da"}
    assert probe.offsets == [{"offset": 12.0}]


def test_no_match_is_a_probe_too(tmp_path):
    s, _, _ = sampler(tmp_path)
    probe = s(50.0)
    assert probe.result is None and probe.offsets is None


def test_samples_are_paced_like_an_identify_run(tmp_path):
    s, _, clock = sampler(tmp_path, min_interval=15)
    s(10.0)
    assert clock.slept == []  # the first request goes straight through
    clock.now += 4
    s(20.0)
    assert clock.slept == [11]
    clock.now += 60
    s(30.0)
    assert clock.slept == [11]  # already long enough since the last one


def test_a_failed_lookup_still_counts_towards_the_pacing(tmp_path):
    clock = FakeClock()
    s = ShazamSampler(
        tmp_path / "x.mp3",
        recognize=lambda seg: (_ for _ in ()).throw(RuntimeError("down")),
        slicer=lambda *a: None,
        clock=clock,
        sleep=clock.sleep,
        min_interval=15,
    )
    with pytest.raises(RuntimeError):
        s(1.0)
    with pytest.raises(RuntimeError):
        s(2.0)
    assert clock.slept == [15]


def test_append_probe_creates_a_v2_file_and_then_extends_it(tmp_path):
    path = tmp_path / "set_progress.json"
    first = Probe(t=0.0, window=30.0, purpose="manual", result=None)
    append_probe(path, first, 900.0)
    append_probe(path, Probe(t=60.0, window=30.0, purpose="manual", result={"artist": "A"}), None)
    probes, duration = load_probes(path)
    assert duration == 900.0
    assert [p.t for p in probes] == [0.0, 60.0]
    assert not (tmp_path / "set_progress.json.tmp").exists()


def test_append_probe_converts_a_legacy_sequential_file(tmp_path):
    path = tmp_path / "set_progress.json"
    path.write_text(json.dumps([[0, {"artist": "A", "title": "a"}]]))
    append_probe(path, Probe(t=40.0, window=30.0, purpose="manual", result=None), 600.0)
    probes, duration = load_probes(path)
    assert duration == 600.0
    assert [(p.t, p.purpose) for p in probes] == [(0.0, "coverage"), (40.0, "manual")]


# ---- the endpoint --------------------------------------------------------


@pytest.fixture
def served(sample_tracklist, tmp_path, monkeypatch):
    from setlist_maker import web_editor
    from setlist_maker.web_editor import EditorContext, create_server

    monkeypatch.setattr(web_editor, "probe_duration_seconds", lambda path: 720.0)
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"ID3")
    asked = []

    def fake_sampler(t):
        asked.append(t)
        return Probe(
            t=t - 15,
            window=30.0,
            purpose="manual",
            result={"artist": "Trio", "title": "Da Da Da", "confidence": 1.0},
        )

    ctx = EditorContext(
        tracklist=sample_tracklist,
        output_path=tmp_path / "set_tracklist.md",
        corrections_db=None,
        audio_path=audio,
        progress_path=tmp_path / "set_progress.json",
        sampler=fake_sampler,
    )
    httpd = create_server(ctx)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", ctx, asked
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req)


def test_sample_endpoint_answers_and_keeps_the_probe(served):
    base, ctx, asked = served
    with _post(base, "/api/sample", {"t": 100}) as r:
        data = json.loads(r.read())
    assert asked == [100.0]
    assert data["ok"] is True
    assert data["probe"]["purpose"] == "manual"
    assert data["probe"]["artist"] == "Trio"
    probes, duration = load_probes(ctx.progress_path)
    assert [p.t for p in probes] == [85.0]
    assert duration == 720.0
    with urllib.request.urlopen(base + "/api/probes") as r:
        listed = json.loads(r.read())
    assert listed["can_sample"] is True
    assert listed["probes"][0]["t"] == 85.0


@pytest.mark.parametrize("payload", [{}, {"t": "soon"}, {"t": -5}, {"t": float("nan")}])
def test_sample_endpoint_refuses_a_bad_time(served, payload):
    base, _, asked = served
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/sample", payload)
    assert exc.value.code == 400
    assert asked == []


def test_sample_endpoint_reports_a_failed_lookup(served):
    base, ctx, _ = served

    def broken(t):
        raise RuntimeError("Shazam unreachable")

    ctx.sampler = broken
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/sample", {"t": 10})
    assert exc.value.code == 502
    assert "unreachable" in json.loads(exc.value.read())["error"]
    assert not ctx.progress_path.exists()


def test_sampling_is_unavailable_without_audio(served):
    base, ctx, asked = served
    ctx.audio_path = None
    with urllib.request.urlopen(base + "/api/probes") as r:
        assert json.loads(r.read())["can_sample"] is False
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/sample", {"t": 10})
    assert exc.value.code == 409
    assert asked == []


def test_sample_endpoint_rejects_a_foreign_host(served):
    base, _, asked = served
    req = urllib.request.Request(
        base + "/api/sample", method="POST", data=b'{"t": 1}', headers={"Host": "attacker.example"}
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 403
    assert asked == []


def test_the_editor_sampler_is_paced_from_the_runs_last_call(tmp_path):
    clock = FakeClock()
    s, _, _ = sampler(tmp_path, clock=clock, min_interval=15, last_call=clock.now - 5)
    s(10.0)
    assert clock.slept == [10]


def test_sample_endpoint_refuses_a_time_past_the_end(served):
    base, _, asked = served
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/sample", {"t": 721})
    assert exc.value.code == 400
    assert asked == []
