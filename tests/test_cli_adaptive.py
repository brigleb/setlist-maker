"""CLI: budget parsing and adaptive-vs-sequential routing."""

import argparse

import pytest

import setlist_maker.cli as cli
from setlist_maker.cli import parse_budget


def test_parse_budget_units():
    assert parse_budget("2h") == 7200.0
    assert parse_budget("45m") == 2700.0
    assert parse_budget("90s") == 90.0
    assert parse_budget("30") == 1800.0  # bare number = minutes


def test_parse_budget_rejects_junk():
    for bad in ("", "h", "2x", "-5m"):
        with pytest.raises(ValueError):
            parse_budget(bad)


def _args(tmp_path, **over):
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"\x00")
    defaults = dict(
        path=str(audio),
        edit=False,
        web_edit=False,
        chapters=False,
        cover=None,
        no_artwork=False,
        reidentify=False,
        no_resume=False,
        allow_partial=False,
        no_learn=True,
        no_summary=True,
        no_panel=True,
        output_dir=None,
        delay=0,
        title_threshold=0.85,
        artist_threshold=0.9,
        singleton_confidence=0.6,
        no_smoothing=False,
        sequential=False,
        precision=5.0,
        budget=None,
        stride=90.0,
        refine_window=12.0,
        call_log=None,
        no_call_log=False,
    )
    defaults.update(over)
    return argparse.Namespace(**defaults)


def _capture(monkeypatch):
    seen = {}

    async def fake_adaptive(**kwargs):
        seen["adaptive"] = kwargs
        return None

    async def fake_sequential(**kwargs):
        seen["sequential"] = kwargs
        return None

    monkeypatch.setattr(cli, "process_single_file_adaptive", fake_adaptive)
    monkeypatch.setattr(cli, "process_single_file", fake_sequential)
    return seen


def test_default_routes_to_adaptive(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):  # fake returns None -> CLI exits 1
        cli.cmd_identify(_args(tmp_path))
    assert "adaptive" in seen and "sequential" not in seen
    cfg = seen["adaptive"]["engine_config"]
    assert cfg.precision == 5.0 and cfg.stride == 90.0


def test_sequential_flag_routes_to_old_path(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, sequential=True))
    assert "sequential" in seen and "adaptive" not in seen


def test_budget_is_parsed_and_forwarded(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, budget="2h"))
    assert seen["adaptive"]["budget_seconds"] == 7200.0


def test_no_smoothing_warns_under_adaptive(tmp_path, monkeypatch, capsys):
    _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, no_smoothing=True))
    assert "--sequential" in capsys.readouterr().out


def test_bad_precision_fails_fast(tmp_path, monkeypatch):
    _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, precision=0.0))


def test_shared_flags_forward_to_the_adaptive_driver(tmp_path, monkeypatch):
    """The flags that are not mode-specific must reach the *default* pipeline.

    tests/test_cli.py asserts these against the sequential driver, which is no
    longer the default -- without this, every one of them could stop being
    forwarded on an ordinary run and the suite would stay green.
    """
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, no_summary=True, allow_partial=True, no_resume=True))
    kwargs = seen["adaptive"]
    assert kwargs["summary"] is False
    assert kwargs["allow_partial"] is True
    assert kwargs["resume"] is False
    assert kwargs["delay_seconds"] == 0


def test_engine_config_carries_the_detection_tuning_flags(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(
            _args(
                tmp_path,
                title_threshold=0.7,
                artist_threshold=0.95,
                singleton_confidence=0.4,
                precision=8.0,
                stride=120.0,
                refine_window=15.0,
            )
        )
    cfg = seen["adaptive"]["engine_config"]
    assert cfg.title_threshold == 0.7
    assert cfg.artist_threshold == 0.95
    assert cfg.singleton_confidence_keep == 0.4
    assert cfg.precision == 8.0 and cfg.stride == 120.0 and cfg.refine_window == 15.0


def test_call_log_still_reaches_the_default_pipeline(tmp_path, monkeypatch):
    """Adaptive is the default, so a call log wired only into --sequential
    would be empty for every ordinary run -- which is exactly when a
    throttling question gets asked."""
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path))
    assert seen["adaptive"]["call_log"].name == "setlist-maker-calls.jsonl"

    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, no_call_log=True))
    assert seen["adaptive"]["call_log"] is None


def test_stride_must_exceed_refine_window(tmp_path, monkeypatch, capsys):
    _capture(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_identify(_args(tmp_path, stride=10.0, refine_window=12.0))
    assert "--stride" in capsys.readouterr().out


# --- resume hint -----------------------------------------------------------
# A stopped adaptive run always leaves a tracklist behind (that is the anytime
# property working), so the reuse short-circuit fires on the *normal* way of
# continuing one -- and the flag that continues it is called --reidentify.
# Without a hint, the CLI's only advice reads as "throw it away and start over".

_MD = """# Tracklist: set.mp3

*Generated on 2026-01-01 00:00*

1. **Amber** - Yeoman (0:00)
"""


def _seed_existing(tmp_path, *, duration=1800.0, probes=None, legacy=False):
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"\x00")
    (tmp_path / "set_tracklist.md").write_text(_MD)
    if probes is not None:
        import json

        if legacy:
            payload = probes
        else:
            payload = {"version": 2, "audio_duration": duration, "probes": probes}
        (tmp_path / "set_progress.json").write_text(json.dumps(payload))
    return audio


def _probe(t, window=30.0):
    return {
        "t": t,
        "window": window,
        "purpose": "coverage",
        "result": {"artist": "Amber", "title": "Yeoman", "confidence": 0.9},
        "offsets": None,
    }


def test_reuse_reports_unfinished_adaptive_work(tmp_path, capsys):
    _seed_existing(tmp_path, probes=[_probe(885.0)])
    cli.cmd_identify(_args(tmp_path))
    out = capsys.readouterr().out
    assert "Found existing tracklist" in out
    assert "unfinished adaptive run" in out
    assert "1 probe" in out and "remaining" in out
    assert "--reidentify continues it" in out
    assert "--no-resume" in out


def test_no_hint_when_the_saved_run_already_converged(tmp_path, capsys):
    # 60s of audio, one 30s probe centred on it: nothing left above target.
    _seed_existing(tmp_path, duration=60.0, probes=[_probe(15.0)])
    cli.cmd_identify(_args(tmp_path))
    out = capsys.readouterr().out
    assert "Found existing tracklist" in out
    assert "unfinished" not in out


def test_no_hint_without_a_progress_file(tmp_path, capsys):
    _seed_existing(tmp_path)
    cli.cmd_identify(_args(tmp_path))
    assert "unfinished" not in capsys.readouterr().out


def test_no_hint_when_resume_is_disabled_or_sequential(tmp_path, capsys):
    _seed_existing(tmp_path, probes=[_probe(885.0)])
    cli.cmd_identify(_args(tmp_path, no_resume=True))
    assert "unfinished" not in capsys.readouterr().out
    cli.cmd_identify(_args(tmp_path, sequential=True))
    assert "unfinished" not in capsys.readouterr().out


def test_legacy_progress_is_described_without_an_estimate(tmp_path, capsys):
    """A v1 file carries no duration, so remaining work cannot be estimated --
    but it still converts and resumes, so it must still be mentioned."""
    _seed_existing(tmp_path, legacy=True, probes=[[0, None], [30, None]])
    cli.cmd_identify(_args(tmp_path))
    out = capsys.readouterr().out
    assert "2 saved sample" in out and "--reidentify" in out


def test_unreadable_progress_never_breaks_the_reuse_path(tmp_path, capsys):
    (tmp_path / "set_progress.json").write_text("{ not json")
    _seed_existing(tmp_path)
    cli.cmd_identify(_args(tmp_path))  # must not raise
    assert "Found existing tracklist" in capsys.readouterr().out
