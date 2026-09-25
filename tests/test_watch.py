"""`identify --watch`: the live timeline, the questions it asks the run, and the
hand-over to the editor when the run finishes."""

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import setlist_maker.adaptive as adaptive
import setlist_maker.cli as cli
import setlist_maker.web_editor as web_editor
from setlist_maker.adaptive import live_snapshot, load_probes, process_single_file_adaptive
from setlist_maker.boundary import Probe
from setlist_maker.editor import Track, Tracklist
from setlist_maker.sampler import SampleRequests, SampleRequestsClosed
from setlist_maker.web_editor import WatchSession
from tests.test_adaptive_driver import _oracle, _wire
from tests.test_cli_adaptive import _args


def _in_thread(fn, *args):
    box = {}

    def run():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # handed back to the test thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---- the request queue ------------------------------------------------------


def test_a_request_blocks_until_the_run_answers_it():
    requests = SampleRequests()
    thread, box = _in_thread(requests.ask, 120.0)
    assert _wait(requests.pending)
    request = requests.pop()
    assert request.t == 120.0
    probe = Probe(t=105.0, window=30.0, purpose="manual", result=None)
    requests.resolve(request, probe)
    thread.join(2)
    assert box["value"] is probe


def test_closing_fails_the_waiting_and_refuses_the_rest():
    requests = SampleRequests()
    thread, box = _in_thread(requests.ask, 10.0)
    assert _wait(requests.pending)
    requests.close("done")
    thread.join(2)
    assert isinstance(box["error"], SampleRequestsClosed)
    with pytest.raises(SampleRequestsClosed):
        requests.ask(20.0)


def test_an_unanswered_request_times_out_and_leaves_the_queue():
    requests = SampleRequests()
    with pytest.raises(TimeoutError):
        requests.ask(5.0, timeout=0.05)
    assert not requests.pending()


# ---- the driver serves them -------------------------------------------------


def test_the_run_answers_a_question_before_its_own_next_probe(tmp_path, monkeypatch):
    oracle = _oracle()
    _wire(monkeypatch, oracle)
    requests = SampleRequests()
    thread, box = _in_thread(requests.ask, 450.0)
    assert _wait(requests.pending)

    result = asyncio.run(
        process_single_file_adaptive(
            audio_path=tmp_path / "set.mp3",
            output_dir=None,
            delay_seconds=0,
            summary=False,
            requests=requests,
        )
    )
    thread.join(2)

    probes, _ = load_probes(tmp_path / "set_progress.json")
    first = probes[0]
    # Centred on the moment asked about, and taken first.
    assert (first.t, first.window, first.purpose) == (435.0, 30.0, "manual")
    assert box["value"].purpose == "manual"
    assert box["value"].result["title"] == "Beta"
    # Evidence like any other: the run still finds all three tracks.
    assert [t.title for t in result[0].tracks] == ["Alpha", "Beta", "Gamma"]
    # And nothing will answer a question asked once it has finished.
    with pytest.raises(SampleRequestsClosed):
        requests.ask(10.0)


def test_live_snapshot_is_what_the_run_would_write(tmp_path, monkeypatch):
    oracle = _oracle()
    _wire(monkeypatch, oracle)
    asyncio.run(
        process_single_file_adaptive(
            audio_path=tmp_path / "set.mp3", output_dir=None, delay_seconds=0, summary=False
        )
    )
    tracklist, plan, probes, duration = live_snapshot(tmp_path / "set_progress.json", "set.mp3")
    assert [t.title for t in tracklist.tracks] == ["Alpha", "Beta", "Gamma"]
    assert plan is None  # converged: nowhere left to look
    assert duration == 900.0 and probes


def test_live_snapshot_of_a_run_not_started_is_empty(tmp_path):
    tracklist, plan, probes, duration = live_snapshot(tmp_path / "none.json", "set.mp3")
    assert tracklist.tracks == [] and plan is None and probes == []


def test_a_progress_write_is_never_seen_half_done(tmp_path):
    path = tmp_path / "set_progress.json"
    adaptive.save_progress_v2(
        900.0, [Probe(t=0.0, window=30.0, purpose="coverage", result=None)], path
    )
    assert json.loads(path.read_text())["version"] == 2
    assert not (tmp_path / "set_progress.json.tmp").exists()


# ---- the watch server -------------------------------------------------------


def _get(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def _post(url, payload):
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


@pytest.fixture
def watching(tmp_path, monkeypatch):
    """A watch session over a half-finished run's progress file."""
    monkeypatch.setattr(web_editor, "probe_duration_seconds", lambda path: 900.0)
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"ID3")
    probes = [
        Probe(t=0.0, window=30.0, purpose="coverage", result={"artist": "A", "title": "Alpha"}),
        Probe(t=400.0, window=30.0, purpose="coverage", result={"artist": "B", "title": "Beta"}),
    ]
    adaptive.save_progress_v2(900.0, probes, tmp_path / "set_progress.json")
    session = WatchSession(audio, tmp_path / "set_tracklist.md", open_browser=False)
    try:
        yield session, tmp_path
    finally:
        if session.thread.is_alive():
            session.stop()


def test_the_live_page_follows_the_progress_file(watching):
    session, tmp_path = watching
    data = _get(session.url + "api/tracklist")
    assert data["live"] is True
    assert [t["title"] for t in data["tracks"]] == ["Alpha", "Beta"]
    probes = _get(session.url + "api/probes")
    assert probes["live"] is True and probes["can_sample"] is True
    assert probes["next"] is not None  # a run this sparse has more to do
    # The run records another probe; the next poll sees it.
    more = load_probes(tmp_path / "set_progress.json")[0] + [
        Probe(t=800.0, window=30.0, purpose="coverage", result={"artist": "C", "title": "Gamma"})
    ]
    adaptive.save_progress_v2(900.0, more, tmp_path / "set_progress.json")
    assert len(_get(session.url + "api/probes")["probes"]) == 3


def test_saving_is_refused_while_the_run_can_still_overwrite_it(watching):
    session, tmp_path = watching
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(session.url + "api/save", {"tracks": []})
    assert exc.value.code == 409
    assert not (tmp_path / "set_tracklist.md").exists()


def test_a_live_click_is_answered_by_the_run_not_a_second_caller(watching):
    session, tmp_path = watching
    thread, box = _in_thread(_post, session.url + "api/sample", {"t": 300})
    assert _wait(session.requests.pending)
    request = session.requests.pop()
    session.requests.resolve(
        request,
        Probe(t=285.0, window=30.0, purpose="manual", result={"artist": "B", "title": "Beta"}),
    )
    thread.join(2)
    assert box["value"]["probe"]["t"] == 285.0
    # The run saves its own probes; the server must not append a second copy.
    assert len(load_probes(tmp_path / "set_progress.json")[0]) == 2


def test_finishing_turns_the_same_page_into_the_editor(watching, monkeypatch):
    session, tmp_path = watching
    monkeypatch.setattr(web_editor, "CorrectionsDB", lambda: None)
    finished = Tracklist(
        source_file="set.mp3",
        tracks=[
            Track(timestamp=0, artist="A", title="Alpha"),
            Track(timestamp=400, artist="B", title="Beta"),
        ],
    )
    thread, _box = _in_thread(
        session.finish, finished, tmp_path / "set_tracklist.md", False, tmp_path / "set.mp3"
    )
    assert _wait(lambda: _get(session.url + "api/probes")["live"] is False)
    data = _get(session.url + "api/tracklist")
    assert data["live"] is False and len(data["tracks"]) == 2
    assert _post(session.url + "api/save", {"tracks": []})["ok"] is True
    assert (tmp_path / "set_tracklist.md").exists()
    _post(session.url + "api/done", {})
    thread.join(3)
    assert not thread.is_alive()


def test_done_during_the_run_skips_the_editor(watching, monkeypatch):
    session, tmp_path = watching
    _post(session.url + "api/done", {})
    assert _wait(lambda: not session.thread.is_alive())
    # finish() must not block waiting on a server that is already gone.
    session.finish(Tracklist(source_file="set.mp3"), tmp_path / "set_tracklist.md", False)


# ---- the CLI ----------------------------------------------------------------


class FakeWatch:
    instances = []

    def __init__(self, audio_path, output_path, engine_config=None, sampling=True):
        self.requests = SampleRequests() if sampling else None
        self.sampling = sampling
        self.stopped = self.finished = False
        FakeWatch.instances.append(self)

    def stop(self):
        self.stopped = True

    def finish(self, tracklist, output_path, use_corrections=True, audio_path=None):
        self.finished = True


@pytest.fixture
def fake_watch(monkeypatch):
    FakeWatch.instances = []
    monkeypatch.setattr(cli, "WatchSession", FakeWatch)
    return FakeWatch


def test_watch_and_edit_are_exclusive(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, watch=True, edit=True))
    assert "--watch" in capsys.readouterr().out


def test_watch_hands_its_questions_to_the_adaptive_run(tmp_path, monkeypatch, fake_watch):
    seen = {}

    async def fake_adaptive(**kwargs):
        seen.update(kwargs)
        return Tracklist(source_file="set.mp3", tracks=[Track(0, "A", "Alpha")]), tmp_path / "x.md"

    monkeypatch.setattr(cli, "process_single_file_adaptive", fake_adaptive)
    cli.cmd_identify(_args(tmp_path, watch=True))
    watch = fake_watch.instances[0]
    assert seen["requests"] is watch.requests
    assert watch.finished and not watch.stopped


def test_a_failed_run_stops_the_watch_page(tmp_path, monkeypatch, fake_watch):
    async def failing(**kwargs):
        return None

    monkeypatch.setattr(cli, "process_single_file_adaptive", failing)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, watch=True))
    assert fake_watch.instances[0].stopped


def test_a_sequential_run_is_watched_without_click_to_sample(tmp_path, monkeypatch, fake_watch):
    async def failing(**kwargs):
        return None

    monkeypatch.setattr(cli, "process_single_file", failing)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, watch=True, sequential=True))
    assert fake_watch.instances[0].sampling is False


def test_watch_on_a_finished_set_just_opens_the_web_editor(tmp_path, monkeypatch, fake_watch):
    audio = tmp_path / "set.mp3"
    (tmp_path / "set_tracklist.md").write_text(
        "# Tracklist: set.mp3\n\n1. **A** - Alpha (0:00)\n2. **B** - Beta (3:00)\n"
    )
    opened = {}
    monkeypatch.setattr(cli, "run_web_editor", lambda *a, **k: opened.update(k))
    cli.cmd_identify(_args(tmp_path, watch=True))
    assert fake_watch.instances == []  # nothing is running, so nothing to watch
    assert opened["audio_path"] == audio


# ---- review regressions -----------------------------------------------------


def test_a_slow_live_read_cannot_put_the_live_guess_back_after_finish(watching, monkeypatch):
    """A GET in flight across finish() must not overwrite the finished tracklist:
    the live guess has no summary, and the next Save would drop it for good."""
    session, tmp_path = watching
    real = web_editor.live_snapshot
    entered = threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        time.sleep(0.4)
        return real(*args, **kwargs)

    monkeypatch.setattr(web_editor, "live_snapshot", slow)
    monkeypatch.setattr(web_editor, "CorrectionsDB", lambda: None)
    # Touch the file so the cache misses and the next read replays slowly.
    adaptive.save_progress_v2(
        900.0, load_probes(tmp_path / "set_progress.json")[0], tmp_path / "set_progress.json"
    )
    reader, _ = _in_thread(_get, session.url + "api/tracklist")
    assert entered.wait(2)
    final = Tracklist(source_file="set.mp3", tracks=[Track(0, "A", "Alpha")], summary="THE SUMMARY")
    editor, _ = _in_thread(session.finish, final, tmp_path / "set_tracklist.md", False)
    reader.join(3)
    assert session.ctx.tracklist is final
    assert _get(session.url + "api/tracklist")["summary"] == "THE SUMMARY"
    _post(session.url + "api/done", {})
    editor.join(3)


def test_a_live_click_past_the_end_is_refused(watching):
    session, _ = watching
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(session.url + "api/sample", {"t": 901})
    assert exc.value.code == 400
    assert not session.requests.pending()


def test_a_cross_site_text_post_cannot_sample(watching):
    session, _ = watching
    req = urllib.request.Request(
        session.url + "api/sample",
        method="POST",
        data=b'{"t": 10}',
        headers={"Content-Type": "text/plain"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 415
    assert not session.requests.pending()


def test_an_aborted_run_answers_the_question_it_had_taken(tmp_path, monkeypatch):
    oracle = _oracle()
    _wire(monkeypatch, oracle)

    async def boom(*args, **kwargs):
        raise KeyboardInterrupt  # the second Ctrl-C, mid-lookup

    monkeypatch.setattr(adaptive, "identify_sample_with_retry", boom)
    requests = SampleRequests()
    thread, box = _in_thread(requests.ask, 450.0)
    assert _wait(requests.pending)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            process_single_file_adaptive(
                audio_path=tmp_path / "set.mp3",
                output_dir=None,
                delay_seconds=0,
                summary=False,
                requests=requests,
            )
        )
    thread.join(2)
    assert isinstance(box["error"], SampleRequestsClosed)


def test_watch_on_a_markdown_tracklist_opens_the_browser_editor(tmp_path, monkeypatch):
    md = tmp_path / "set_tracklist.md"
    md.write_text("# Tracklist: set.mp3\n\n1. **A** - Alpha (0:00)\n")
    opened = []
    monkeypatch.setattr(cli, "run_web_editor", lambda *a, **k: opened.append("web"))
    monkeypatch.setattr(cli, "run_editor", lambda *a, **k: opened.append("tui"))
    cli.cmd_identify(_args(tmp_path, path=str(md), watch=True))
    assert opened == ["web"]
