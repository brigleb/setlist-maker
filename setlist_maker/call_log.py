"""Per-call telemetry for an identify run.

`identify` cannot tell a rate-limited sample from a genuinely unidentified one.
shazamio retries HTTP 429 internally -- `ExponentialRetry(attempts=20,
max_timeout=60, statuses={..., 429})` in its own constructor -- so a throttle
that is absorbed and recovered from never reaches the caller at all, and one
that is *not* recovered from arrives as `FailedDecodeJson("Failed to decode
json")`, a frozen literal carrying no status code. Neither shape is
distinguishable from "Shazam does not know this song", so a throttled run
yields a confident-looking tracklist with silent holes in it.

This module observes what the caller cannot see and appends it to a JSONL file,
so a few ordinary runs can be reviewed afterwards for evidence of throttling.
It changes no behaviour: nothing here retries, paces or fails a run.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from setlist_maker import __version__


def _now() -> str:
    """UTC ISO-8601, so lines from runs on different days sort and diff cleanly."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CallRecorder:
    """Records every HTTP attempt shazamio makes, including retried-away ones.

    shazamio builds an aiohttp `TraceConfig` and passes it to every request
    (`shazamio/client.py`), but subscribes only to `on_request_start`.
    `on_request_end` is free, and it is handed the live `ClientResponse` -- so
    subscribing to it observes each attempt's status and headers without
    replacing the HTTP client or altering a single retry decision.
    """

    def __init__(self) -> None:
        self._attempts: list[dict[str, Any]] = []

    # ---- wiring ----------------------------------------------------------
    def attach(self, shazam: Any) -> bool:
        """Subscribe to `shazam`'s trace config. False if the seam is missing.

        Guarded rather than assumed: `http_client.trace_config` is shazamio's
        internal detail, and a future release may move it. Losing the log is an
        acceptable degradation; failing the run over it is not.
        """
        try:
            trace_config = shazam.http_client.trace_config
            trace_config.on_request_start.append(self._on_start)
            trace_config.on_request_end.append(self._on_end)
        except AttributeError:
            return False
        return True

    # ---- aiohttp trace callbacks ----------------------------------------
    async def _on_start(self, _session, ctx, _params) -> None:
        ctx.setlist_maker_started = time.monotonic()

    async def _on_end(self, _session, ctx, params) -> None:
        started = getattr(ctx, "setlist_maker_started", None)
        request_ctx = getattr(ctx, "trace_request_ctx", None) or {}
        self._attempts.append(
            {
                "status": params.response.status,
                "attempt": request_ctx.get("current_attempt"),
                "retry_after": params.response.headers.get("Retry-After"),
                "elapsed_s": round(time.monotonic() - started, 3) if started else 0.0,
            }
        )

    # ---- readout ---------------------------------------------------------
    def drain(self) -> list[dict[str, Any]]:
        """Return the attempts seen since the last drain, and forget them."""
        attempts, self._attempts = self._attempts, []
        return attempts


LOG_FILENAME = "setlist-maker-calls.jsonl"


class CallLog:
    """Append-only JSONL sink, one line per Shazam call.

    Deliberately opens and closes per line rather than holding a handle for the
    hour a run takes: the volume is one small line every ~15 seconds, and an
    interrupted run then leaves a complete, readable file behind.

    Every write is best-effort, in the spirit of `summary.py` -- a fault warns
    once and disables the log for the rest of the run. Telemetry that can fail a
    run is worse than no telemetry.
    """

    def __init__(self, path: Path):
        self.path = path
        self.run_id = uuid.uuid4().hex[:8]
        self._disabled = False

    def _write(self, record: dict[str, Any]) -> None:
        if self._disabled:
            return
        record = {"type": record.pop("type"), "run": self.run_id, "ts": _now(), **record}
        try:
            with open(self.path, "a") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            self._disabled = True
            print(f"  Warning: call log disabled ({self.path}): {exc}")

    def write_run(self, *, source: str, total: int, delay_seconds: int, resumed_from: int) -> None:
        self._write(
            {
                "type": "run",
                "version": __version__,
                "source": source,
                "total": total,
                "delay_s": delay_seconds,
                "resumed_from": resumed_from,
            }
        )

    def write_call(
        self,
        *,
        index: int,
        total: int,
        position_seconds: int,
        delay_seconds: int,
        duration_s: float,
        track_info: dict | None,
        attempts: list[dict[str, Any]],
        error: dict[str, Any] | None,
    ) -> None:
        track = track_info or {}
        self._write(
            {
                "type": "call",
                "i": index,
                "total": total,
                "pos_s": position_seconds,
                "delay_s": delay_seconds,
                "dur_s": round(duration_s, 3),
                "matched": bool(track_info),
                "artist": track.get("artist"),
                "title": track.get("title"),
                "conf": track.get("confidence"),
                "attempts": len(attempts),
                # A 429 anywhere in the attempt list means we were throttled --
                # including one shazamio retried away, which is invisible to the
                # caller and is the single fact this whole file exists to catch.
                # The error's own status is checked too: it is the only evidence
                # left if the trace seam ever moves and `attempts` comes back
                # empty, and this is the field a review scans for.
                "throttled": any(a.get("status") == 429 for a in attempts)
                or (error or {}).get("status") == 429,
                "http": attempts,
                "error": error,
            }
        )


def describe_error(exc: BaseException) -> dict[str, Any]:
    """Classify a failed recognition by exception *type*, never by substring.

    The message is recorded but must not be parsed: shazamio's rate-limit shape
    is the frozen literal "Failed to decode json", which contains no status, and
    a production tracker that grepped raw output for "429" misclassified real
    matches whose track ids happened to contain those digits.

    Where aiohttp's `ContentTypeError` is chained (`raise ... from e`), it
    carries the status and headers the message threw away -- the only branch on
    which a 429 is recoverable after the fact.
    """
    cause = exc.__cause__
    headers = getattr(cause, "headers", None) or {}
    return {
        "type": type(exc).__name__,
        "message": str(exc)[:300],
        "status": getattr(cause, "status", None),
        "retry_after": headers.get("Retry-After"),
    }


def call_log_path(audio_path: Path, output_dir: Path | None) -> Path:
    """Where the log lives: beside the audio, or in `output_dir` when given.

    Mirrors `identify.tracklist_output_path()` so every artifact of a run lands
    together. One shared filename rather than a per-file one, because the log is
    reviewed across several runs.
    """
    directory = output_dir if output_dir is not None else audio_path.parent
    return directory / LOG_FILENAME
