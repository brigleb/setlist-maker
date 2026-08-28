"""Tests for the per-call telemetry log (setlist_maker.call_log)."""

import asyncio
import json

from aiohttp import web
from aiohttp_retry import ExponentialRetry
from shazamio import Shazam
from shazamio.client import HTTPClient
from shazamio.exceptions import FailedDecodeJson

from setlist_maker import call_log as call_log_module
from setlist_maker.call_log import (
    LOG_FILENAME,
    CallLog,
    CallRecorder,
    call_log_path,
    describe_error,
)


def _serve(handler):
    """Run `handler` on a loopback server, returning (url, runner) to the caller."""

    async def start():
        app = web.Application()
        app.router.add_post("/tag", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/tag", runner

    return start


def test_recorder_captures_429s_shazamio_retried_away():
    """The whole point: a throttle absorbed by shazamio's internal retries is
    invisible to the caller, but must still land in the log."""
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return web.Response(
                status=429,
                text="<html>Too Many Requests</html>",
                content_type="text/html",
                headers={"Retry-After": "120"},
            )
        return web.json_response({"track": {"title": "Autobahn"}})

    async def scenario():
        url, runner = await _serve(handler)()
        shazam = Shazam()
        recorder = CallRecorder()
        assert recorder.attach(shazam) is True

        result = await shazam.http_client.request("POST", url, json={})
        attempts = recorder.drain()
        await runner.cleanup()
        return result, attempts

    result, attempts = asyncio.run(scenario())

    # The caller sees an ordinary success and learns nothing about the throttle.
    assert result == {"track": {"title": "Autobahn"}}

    # The recorder saw all three attempts.
    assert [a["status"] for a in attempts] == [429, 429, 200]
    assert [a["attempt"] for a in attempts] == [1, 2, 3]
    assert attempts[0]["retry_after"] == "120"
    assert attempts[2]["retry_after"] is None
    assert all(isinstance(a["elapsed_s"], float) for a in attempts)


def test_drain_clears_the_buffer():
    recorder = CallRecorder()
    recorder._attempts.append({"status": 200})
    assert recorder.drain() == [{"status": 200}]
    assert recorder.drain() == []


def test_attach_reports_failure_when_the_shazamio_seam_is_missing():
    """A shazamio release that moves `http_client.trace_config` must cost the
    log, never the run."""

    class Moved:
        http_client = object()

    assert CallRecorder().attach(Moved()) is False


def test_shazamio_still_exposes_the_trace_seam():
    """Canary. `on_request_end` is shazamio's internal detail, not public API --
    if an upgrade moves it, fail here loudly rather than log nothing quietly.

    `Shazam()` performs no network I/O at construction, so this stays inside the
    outbound-network guard in conftest.
    """
    trace_config = Shazam().http_client.trace_config
    assert hasattr(trace_config, "on_request_end")
    assert hasattr(trace_config, "on_request_start")


# --------------------------------------------------------------------------
# CallLog -- the JSONL writer
# --------------------------------------------------------------------------
def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_call_log_writes_a_run_header_then_one_line_per_call(tmp_path):
    path = tmp_path / "calls.jsonl"
    log = CallLog(path)
    log.write_run(source="set.mp3", total=205, delay_seconds=15, resumed_from=0)
    log.write_call(
        index=41,
        total=205,
        position_seconds=1200,
        delay_seconds=15,
        duration_s=3.12,
        track_info={"artist": "Kraftwerk", "title": "Autobahn", "confidence": 0.83},
        attempts=[{"status": 429, "attempt": 1, "retry_after": "120", "elapsed_s": 0.2}],
        error=None,
    )

    run, call = _lines(path)
    assert run["type"] == "run"
    assert run["source"] == "set.mp3"
    assert run["delay_s"] == 15
    assert run["total"] == 205

    assert call["type"] == "call"
    assert call["run"] == run["run"]  # same run id ties the lines together
    assert call["i"] == 41
    assert call["pos_s"] == 1200
    assert call["dur_s"] == 3.12
    assert call["matched"] is True
    assert call["artist"] == "Kraftwerk"
    assert call["conf"] == 0.83
    assert call["http"] == [{"status": 429, "attempt": 1, "retry_after": "120", "elapsed_s": 0.2}]
    assert call["throttled"] is True  # a 429 appeared, even though the call matched
    assert call["error"] is None


def test_an_unmatched_call_is_recorded_without_track_fields(tmp_path):
    path = tmp_path / "calls.jsonl"
    log = CallLog(path)
    log.write_call(
        index=2,
        total=9,
        position_seconds=30,
        delay_seconds=15,
        duration_s=2.5,
        track_info=None,
        attempts=[{"status": 200, "attempt": 1, "retry_after": None, "elapsed_s": 2.4}],
        error=None,
    )
    (call,) = _lines(path)
    assert call["matched"] is False
    assert call["artist"] is None
    assert call["throttled"] is False


def test_runs_accumulate_in_one_file_with_distinct_run_ids(tmp_path):
    """The file is reviewed after several ordinary runs, so it must append."""
    path = tmp_path / "calls.jsonl"
    for _ in range(2):
        log = CallLog(path)
        log.write_run(source="set.mp3", total=1, delay_seconds=15, resumed_from=0)
        log.write_call(
            index=1,
            total=1,
            position_seconds=0,
            delay_seconds=15,
            duration_s=1.0,
            track_info=None,
            attempts=[],
            error=None,
        )

    lines = _lines(path)
    assert len(lines) == 4
    assert len({line["run"] for line in lines}) == 2


def test_an_unwritable_log_warns_once_and_never_breaks_the_run(tmp_path, capsys):
    """Best-effort, like summary.py: a logging fault must not cost a tracklist."""
    log = CallLog(tmp_path / "missing-dir" / "calls.jsonl")
    for i in range(3):
        log.write_call(
            index=i,
            total=3,
            position_seconds=0,
            delay_seconds=15,
            duration_s=1.0,
            track_info=None,
            attempts=[],
            error=None,
        )

    assert capsys.readouterr().out.lower().count("call log") == 1


# --------------------------------------------------------------------------
# describe_error -- classify by type, never by substring
# --------------------------------------------------------------------------
def test_a_real_html_429_yields_type_and_status_not_a_substring_match():
    """The real-world throttle shape: Shazam serves 429 as a small text/html
    page, aiohttp raises ContentTypeError, shazamio re-raises it as
    FailedDecodeJson('Failed to decode json') -- a frozen literal containing no
    status code. The status survives only on the chained cause."""

    async def handler(request):
        return web.Response(
            status=429,
            text="<html>Too Many Requests</html>",
            content_type="text/html",
            headers={"Retry-After": "30"},
        )

    async def scenario():
        url, runner = await _serve(handler)()
        # attempts=1 with 429 outside `statuses`: no retries, so the test does
        # not sit through shazamio's real ~12-minute backoff schedule.
        client = HTTPClient(retry_options=ExponentialRetry(attempts=1, statuses={500}))
        try:
            await client.request("POST", url, json={})
            raise AssertionError("expected FailedDecodeJson")
        except FailedDecodeJson as exc:
            described = describe_error(exc)
        await runner.cleanup()
        return described

    described = asyncio.run(scenario())

    assert described["type"] == "FailedDecodeJson"
    assert described["status"] == 429
    assert described["retry_after"] == "30"
    # The message alone is exactly why substring detection cannot work.
    assert "429" not in described["message"]


def test_an_error_with_no_chained_response_records_type_only():
    described = describe_error(ValueError("Invalid data type"))
    assert described["type"] == "ValueError"
    assert described["message"] == "Invalid data type"
    assert described["status"] is None
    assert described["retry_after"] is None


# --------------------------------------------------------------------------
# where the log lives
# --------------------------------------------------------------------------
def test_the_log_sits_beside_the_audio_by_default(tmp_path):
    assert call_log_path(tmp_path / "set.mp3", None) == tmp_path / LOG_FILENAME


def test_an_output_dir_takes_the_log_with_it(tmp_path):
    out = tmp_path / "out"
    assert call_log_path(tmp_path / "set.mp3", out) == out / LOG_FILENAME


def test_a_429_carried_only_by_the_error_still_counts_as_throttled(tmp_path):
    """`throttled` is the field a review scans for, so it must not depend on the
    HTTP attempt list alone: if the trace seam ever breaks (attach returns
    False) the chained cause's status is the only surviving evidence."""
    path = tmp_path / "calls.jsonl"
    log = CallLog(path)
    log.write_call(
        index=1,
        total=1,
        position_seconds=0,
        delay_seconds=15,
        duration_s=1.0,
        track_info=None,
        attempts=[],
        error={
            "type": "FailedDecodeJson",
            "message": "Failed to decode json",
            "status": 429,
            "retry_after": "30",
        },
    )
    (call,) = _lines(path)
    assert call["throttled"] is True


def test_an_ordinary_failure_is_not_reported_as_throttled(tmp_path):
    path = tmp_path / "calls.jsonl"
    log = CallLog(path)
    log.write_call(
        index=1,
        total=1,
        position_seconds=0,
        delay_seconds=15,
        duration_s=1.0,
        track_info=None,
        attempts=[{"status": 200, "attempt": 1, "retry_after": None, "elapsed_s": 1.0}],
        error={"type": "ValueError", "message": "nope", "status": None, "retry_after": None},
    )
    (call,) = _lines(path)
    assert call["throttled"] is False


def test_an_unserializable_field_is_stringified_rather_than_lost(tmp_path):
    """`describe_error` reads `status` off an arbitrary exception, so nothing
    guarantees it is a number. Losing the whole log over one odd field -- or
    worse, raising out of the sample loop -- is exactly what this module's
    best-effort contract rules out."""
    path = tmp_path / "calls.jsonl"
    log = CallLog(path)
    log.write_call(
        index=1,
        total=1,
        position_seconds=0,
        delay_seconds=15,
        duration_s=1.0,
        track_info=None,
        attempts=[],
        error={"type": "Weird", "message": "?", "status": object(), "retry_after": None},
    )
    (call,) = _lines(path)
    assert call["error"]["type"] == "Weird"
    assert isinstance(call["error"]["status"], str)  # coerced, not dropped


def test_any_serialization_fault_warns_once_and_never_raises(tmp_path, capsys, monkeypatch):
    """Backstop: the guard catches more than OSError, since a write that raises
    out of the loop would cost the tracklist the log exists to protect."""

    def boom(*args, **kwargs):
        raise RuntimeError("json exploded")

    monkeypatch.setattr(call_log_module.json, "dumps", boom)
    log = CallLog(tmp_path / "calls.jsonl")
    for i in range(3):
        log.write_call(
            index=i,
            total=3,
            position_seconds=0,
            delay_seconds=15,
            duration_s=1.0,
            track_info=None,
            attempts=[],
            error=None,
        )

    assert capsys.readouterr().out.lower().count("call log") == 1
