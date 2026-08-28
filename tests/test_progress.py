"""Tests for the live progress panel shown under the identify log."""

from rich.console import Console

from setlist_maker.progress import RunState, format_duration, live_display, render_panel

PANEL_ROWS = 6  # top border, four content lines, bottom border


def make_state(done: int = 0, total: int = 205, **kwargs) -> RunState:
    """A RunState with `done` samples finished, alternating hit/miss in threes."""
    results: list[dict | None] = []
    for i in range(done):
        if i % 7 == 3:
            results.append(None)
        else:
            results.append(
                {
                    "artist": f"Artist {i // 6}",
                    "title": f"Title {i // 6}",
                    "confidence": 0.9,
                }
            )
    state = RunState(
        source_name="set.mp3",
        total_samples=total,
        audio_seconds=total * 30,
        delay_seconds=15,
        clock=lambda: 1000.0,
        **kwargs,
    )
    state.results = results
    state.index = done + 1
    return state


def render_lines(state: RunState, width: int = 80) -> list[str]:
    """Render the panel to plain text and return its lines, blank spacer stripped."""
    console = Console(width=width, record=True, force_terminal=False, no_color=True)
    console.print(render_panel(state, width))
    text = console.export_text(clear=False)
    return [line for line in text.split("\n") if line.strip()]


class TestPanelShape:
    def test_panel_is_six_rows(self):
        assert len(render_lines(make_state(42))) == PANEL_ROWS

    def test_row_count_is_stable_across_every_phase(self):
        for phase in ("identifying", "cooldown", "backoff", "done"):
            state = make_state(42)
            state.phase = phase
            assert len(render_lines(state)) == PANEL_ROWS, phase

    def test_no_line_exceeds_the_width(self):
        for width in (46, 60, 80, 100, 120, 200):
            state = make_state(42)
            for line in render_lines(state, width):
                assert len(line) <= width, (width, line)

    def test_a_long_artist_and_title_are_truncated_not_wrapped(self):
        state = make_state(42)
        state.results[-1] = {
            "artist": "A Very Long Artist Name That Goes On" * 4,
            "title": "And An Equally Interminable Track Title" * 4,
            "confidence": 0.8,
        }
        lines = render_lines(state)
        assert len(lines) == PANEL_ROWS
        assert all(len(line) <= 80 for line in lines)

    def test_a_long_source_name_does_not_break_the_border(self):
        state = make_state(42, total=205)
        state.source_name = "an-absurdly-long-recording-filename-" * 5 + ".mp3"
        lines = render_lines(state)
        assert len(lines) == PANEL_ROWS
        assert all(len(line) <= 80 for line in lines)


class TestPanelContent:
    def test_shows_counter_percent_and_position(self):
        body = "\n".join(render_lines(make_state(42)))
        assert "42/205" in body
        assert "20%" in body
        assert "1:42:30" in body  # total duration

    def test_identifying_names_the_sample_in_flight(self):
        state = make_state(42)
        state.phase = "identifying"
        assert "sample 43" in "\n".join(render_lines(state))

    def test_the_first_frame_never_announces_sample_zero(self):
        """`Live` renders once before the loop calls begin_sample()."""
        state = make_state(0)
        state.index = 0
        body = "\n".join(render_lines(state))
        assert "sample 0" not in body
        assert "sample 1" in body

    def test_cooldown_counts_down_to_the_next_sample(self):
        state = make_state(42)
        state.begin_cooldown(15)
        state.clock = lambda: state.phase_deadline - 12.4
        assert "next sample in 12s" in "\n".join(render_lines(state))

    def test_backoff_shows_the_retry_number_and_countdown(self):
        state = make_state(42)
        state.begin_backoff(30, attempt=2)
        state.clock = lambda: state.phase_deadline - 19.0
        body = "\n".join(render_lines(state))
        assert "rate limited" in body
        assert f"retry 2/{state.max_retries}" in body
        assert "19s" in body

    def test_done_reports_completion_rather_than_an_eta(self):
        state = make_state(205)
        state.finish()
        body = "\n".join(render_lines(state))
        assert "Complete" in body
        assert "finished all 205 samples" in body
        assert "ETA" not in body

    def test_early_eta_is_marked_as_a_guess(self):
        assert "~" in "\n".join(render_lines(make_state(2)))

    def test_settled_eta_is_not_marked_as_a_guess(self):
        state = make_state(60)
        state.started_at = 0.0
        state.clock = lambda: 60 * 18.0
        eta_line = [line for line in render_lines(state) if "ETA" in line][0]
        assert "~" not in eta_line

    def test_no_samples_yet_reads_as_deliberate(self):
        body = "\n".join(render_lines(make_state(0)))
        assert "no samples finished yet" in body
        assert "no match yet" in body

    def test_title_carries_the_phase(self):
        for phase, word in [
            ("identifying", "Identifying"),
            ("cooldown", "Cooling down"),
            ("backoff", "Rate limited"),
            ("done", "Complete"),
        ]:
            state = make_state(42)
            state.phase = phase
            assert word in render_lines(state)[0], phase


class TestHostileMetadata:
    """The panel's height must never change: `Live` erases a fixed number of lines."""

    def test_a_newline_in_a_title_does_not_add_a_row(self):
        state = make_state(42)
        state.results[-1] = {"artist": "A", "title": "Part One\nPart Two", "confidence": 0.9}
        lines = render_lines(state)
        assert len(lines) == PANEL_ROWS
        assert "Part One Part Two" in "\n".join(lines)

    def test_a_newline_in_an_artist_does_not_add_a_row(self):
        state = make_state(42)
        state.results[-1] = {"artist": "A\rB", "title": "T", "confidence": 0.9}
        assert len(render_lines(state)) == PANEL_ROWS

    def test_a_tab_in_a_title_does_not_break_the_rail(self):
        state = make_state(42)
        state.results[-1] = {"artist": "A", "title": "One\t\tTwo", "confidence": 0.9}
        lines = render_lines(state)
        assert len(lines) == PANEL_ROWS
        assert all(len(line) <= 80 for line in lines)

    def test_a_newline_in_the_source_name_does_not_add_a_row(self):
        state = make_state(42)
        state.source_name = "set\nname.mp3"
        assert len(render_lines(state)) == PANEL_ROWS


class TestLongRecordings:
    def test_a_ten_hour_recording_shows_its_position_unellipsized(self):
        """The rail exists to hold the position steady; widen rather than truncate it."""
        state = make_state(600, total=1200)
        state.audio_seconds = 1200 * 30  # 10 hours
        position_line = render_lines(state)[1]
        assert "5:00:00 / 10:00:00" in position_line
        # ...and not "10:00:…". (The spinner row's own ellipsis is legitimate.)
        assert "…" not in position_line

    def test_a_long_recording_still_fits_the_width(self):
        state = make_state(600, total=1200)
        state.audio_seconds = 1200 * 30
        for width in (46, 60, 80, 110, 200):
            lines = render_lines(state, width)
            assert len(lines) == PANEL_ROWS
            assert all(len(line) <= width for line in lines)


class TestBackoffRetry:
    def test_an_expired_backoff_reports_the_retry_not_a_frozen_countdown(self):
        """The phase stays "backoff" while the retry request is in flight."""
        state = make_state(42)
        state.begin_backoff(30, attempt=1)
        state.clock = lambda: state.phase_deadline + 5
        body = "\n".join(render_lines(state))
        assert "in 0s" not in body
        assert "retrying sample 43" in body


class TestDerivedNumbers:
    def test_unique_tracks_counts_distinct_songs_not_samples(self):
        state = make_state(42)
        # make_state groups six samples per track, with a miss in each block of 7
        assert state.unique_tracks == len({(r["artist"], r["title"]) for r in state.results if r})

    def test_hits_and_misses_partition_the_finished_samples(self):
        state = make_state(42)
        assert state.hits + state.misses == state.done == 42

    def test_hit_rate_is_zero_with_no_samples(self):
        assert make_state(0).hit_rate == 0.0

    def test_fraction_is_zero_when_the_total_is_unknown(self):
        assert make_state(0, total=0).fraction == 0.0

    def test_eta_uses_observed_pace_once_enough_samples_have_run(self):
        state = make_state(50)
        state.started_at = 0.0
        state.clock = lambda: 50 * 20.0  # 20s per sample observed
        assert state.eta_seconds == (205 - 50) * 20.0

    def test_eta_ignores_samples_carried_in_by_a_resume(self):
        """A resumed run must not divide this run's elapsed by the cached results."""
        state = make_state(50, resumed_from=40)
        state.started_at = 0.0
        state.clock = lambda: 100.0  # 10 samples in 100s
        assert state.seconds_per_sample == 10.0

    def test_position_is_the_sample_in_flight(self):
        assert make_state(42).sample_seconds == 42 * 30

    def test_current_is_the_most_recent_identified_track(self):
        state = make_state(42)
        state.results.append(None)
        assert state.current is not None
        assert state.current["artist"] == "Artist 6"


class TestPhaseTransitions:
    def test_begin_sample_sets_the_phase_and_index(self):
        state = make_state(10)
        state.begin_sample(11)
        assert state.phase == "identifying"
        assert state.index == 11
        assert state.phase_deadline is None

    def test_record_appends_the_result(self):
        state = make_state(10)
        state.record({"artist": "A", "title": "B", "confidence": 0.5})
        assert state.done == 11
        assert state.current["artist"] == "A"

    def test_phase_remaining_never_goes_negative(self):
        state = make_state(10)
        state.begin_cooldown(15)
        state.clock = lambda: state.phase_deadline + 99
        assert state.phase_remaining == 0.0

    def test_phase_remaining_is_zero_without_a_deadline(self):
        state = make_state(10)
        state.begin_sample(11)
        assert state.phase_remaining == 0.0

    def test_started_at_comes_from_the_injected_clock(self):
        """Otherwise a fake clock is mixed with a real start and elapsed is nonsense."""
        state = RunState(
            source_name="set.mp3",
            total_samples=10,
            audio_seconds=300,
            delay_seconds=15,
            clock=lambda: 500.0,
        )
        assert state.started_at == 500.0
        assert state.elapsed == 0.0

    def test_an_explicit_started_at_is_respected(self):
        state = RunState(
            source_name="set.mp3",
            total_samples=10,
            audio_seconds=300,
            delay_seconds=15,
            clock=lambda: 500.0,
            started_at=440.0,
        )
        assert state.elapsed == 60.0

    def test_the_spinner_advances_with_the_clock(self):
        """The panel animates off rich's refresh thread, so the frame must come
        from the clock rather than from the pipeline ticking a counter."""
        state = make_state(10)
        state.started_at = 0.0
        state.clock = lambda: 0.0
        first = state.tick
        state.clock = lambda: 5.0
        assert state.tick != first


class TestLiveDisplay:
    def test_live_does_not_hijack_stderr(self):
        """`identify set.mp3 2> errors.log` must still capture stderr.

        rich redirects stderr whenever *stdout* is a terminal, which would send
        every warning to the terminal and leave the redirect target empty.
        """
        import sys

        state = make_state(1)
        before = sys.stderr
        with live_display(state, enabled=True):
            during = sys.stderr
        assert during is before

    def test_disabled_display_logs_plainly(self, capsys):
        state = make_state(1)
        with live_display(state, enabled=False) as display:
            display.log("  [1/5]  0:00  + 90%  A - B")
        assert "[1/5]" in capsys.readouterr().out


class TestFormatDuration:
    def test_seconds(self):
        assert format_duration(45) == "45s"

    def test_minutes(self):
        assert format_duration(372) == "6m 12s"

    def test_hours(self):
        assert format_duration(3840) == "1h 04m"

    def test_negative_clamps_to_zero(self):
        assert format_duration(-5) == "0s"
