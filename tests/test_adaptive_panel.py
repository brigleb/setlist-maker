"""Adaptive panel: pure rendering, fixed height, clock-driven motion."""

from rich.console import Console

from setlist_maker.progress import AdaptiveRunState, render_adaptive_panel


def _lines(state, width=80):
    console = Console(width=width, force_terminal=True, color_system=None)
    with console.capture() as cap:
        console.print(render_adaptive_panel(state, width))
    return [line for line in cap.get().splitlines() if line.strip()]


def _state(**over):
    defaults = dict(
        source_name="set.mp3",
        audio_seconds=14400,
        delay_seconds=15,
        probes_done=42,
        hits=39,
        tracks_found=11,
        boundaries_found=10,
        boundaries_at_target=6,
        widest_gap=44.0,
        est_probes_remaining=58,
        current_t=7200.0,
        current_purpose="refine",
        current_result={"artist": "B", "title": "Beta", "confidence": 0.87},
        clock=lambda: 1000.0,
        started_at=900.0,
    )
    defaults.update(over)
    return AdaptiveRunState(**defaults)


def test_panel_height_is_fixed_across_states():
    heights = set()
    for over in (
        {},
        {"phase": "cooldown", "phase_deadline": 1010.0},
        {"phase": "backoff", "phase_deadline": 1030.0, "retry": 2},
        {"phase": "done"},
        {"current_result": None, "probes_done": 0, "boundaries_found": 0},
        {"current_result": {"artist": "X", "title": "line\nbreak", "confidence": 0.5}},
    ):
        heights.add(len(_lines(_state(**over))))
    assert len(heights) == 1  # 6 rendered lines: box top + 4 rows + box bottom


def test_panel_shows_boundary_stats_and_position():
    text = "\n".join(_lines(_state()))
    assert "6/10" in text and "11 tracks" in text
    assert "2:00:00 / 4:00:00" in text


def test_cooldown_counts_down_from_injected_clock():
    state = _state(phase="cooldown", phase_deadline=1012.0)
    assert "12s" in "\n".join(_lines(state))


def test_narrow_terminal_never_wraps():
    for line in _lines(_state(), width=46):
        assert len(line) <= 46
