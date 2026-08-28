"""Live progress panel for the identify run.

`identify` spends most of an hour waiting: a 1h42m set is 205 samples, each one
a ~3s Shazam call followed by a 15s politeness delay that produced no output at
all. This module renders a four-line dashboard, pinned by `rich.live.Live` under
the unchanged scrolling log, that answers "what is it doing, how far in, how
long left" without the user having to infer it from the log's pace.

Two properties are load-bearing:

- **`render_panel()` is a pure function of `RunState` plus a clock**, so it is
  unit-testable at a fixed width with no `Live`, no pty and no timing, exactly
  as `identify.format_progress_line()` is.
- **Everything that moves is derived from the clock, not from a counter the
  pipeline ticks.** `Live` re-renders on its own thread, so a renderable that
  recomputes from `time.monotonic()` animates the cooldown countdown *during*
  `await asyncio.sleep(delay_seconds)` — which is why that sleep in
  `process_single_file` needs no restructuring. Measured: 11 renders during an
  untouched 1.0s sleep.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

from rich.box import ROUNDED
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from setlist_maker.audio import SAMPLE_DURATION_MS, format_timestamp
from setlist_maker.shazam_client import MAX_RETRIES

SAMPLE_SECONDS = SAMPLE_DURATION_MS // 1000

MAX_BOX = 110  # a 200-column terminal does not need a 200-column dashboard
MIN_BOX = 46
RAIL = 17  # fits "1:42:30 / 1:42:30"; widened at render time if the audio is longer
GAP = 2
METER = 8

# Observed pace is noise until a few samples of *this* run have completed; below
# this the ETA is shown with a leading "~".
SETTLED_SAMPLES = 4

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPIN_HZ = 8  # spinner frames per second
# Indexed 1..7: sub-cell progress, so the bar visibly moves on every sample.
_PARTIAL = " ▏▎▍▌▋▊▉"

_ACCENT = "green"
_TRACK = "grey42"  # the unfilled half of any bar
_MUTED = "grey62"

# phase -> (title word, border style, title style). The frame *is* the phase
# indicator: the box changes colour on a rate limit, so the state registers
# before a word is read.
_FRAME = {
    "identifying": ("Identifying", "grey50", "bold"),
    "cooldown": ("Cooling down", "grey50", "bold"),
    "backoff": ("Rate limited", "yellow", "bold yellow"),
    "done": ("Complete", _ACCENT, "bold green"),
}


def format_duration(seconds: float) -> str:
    """Compact human span for ETAs and elapsed time: 45s / 6m 12s / 1h 04m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


@dataclass
class RunState:
    """Everything the panel draws, and the only thing the pipeline has to maintain.

    Nothing here is new information: it is what `process_single_file`'s loop
    already knows, plus a phase label and a deadline.
    """

    source_name: str
    total_samples: int
    audio_seconds: int
    delay_seconds: int
    results: list[dict | None] = field(default_factory=list)
    resumed_from: int = 0
    index: int = 0  # 1-based index of the sample in flight
    phase: str = "identifying"
    phase_deadline: float | None = None
    retry: int = 0
    max_retries: int = MAX_RETRIES
    clock: Callable[[], float] = time.monotonic
    # Read from `clock` in __post_init__ rather than defaulting to
    # time.monotonic directly, so an injected clock makes elapsed/ETA
    # deterministic instead of mixing a fake clock with a real start.
    started_at: float | None = None

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    # ---- transitions, called from the pipeline ---------------------------
    def begin_sample(self, index: int) -> None:
        self.index = index
        self.phase = "identifying"
        self.phase_deadline = None
        self.retry = 0

    def record(self, track_info: dict | None) -> None:
        self.results.append(track_info)

    def begin_cooldown(self, seconds: float) -> None:
        self.phase = "cooldown"
        self.phase_deadline = self.clock() + seconds

    def begin_backoff(self, seconds: float, attempt: int) -> None:
        self.phase = "backoff"
        self.phase_deadline = self.clock() + seconds
        self.retry = attempt

    def finish(self) -> None:
        self.phase = "done"
        self.phase_deadline = None

    # ---- derived ---------------------------------------------------------
    @property
    def done(self) -> int:
        return len(self.results)

    @property
    def hits(self) -> int:
        return sum(1 for r in self.results if r)

    @property
    def misses(self) -> int:
        return self.done - self.hits

    @property
    def hit_rate(self) -> float:
        return self.hits / self.done if self.done else 0.0

    @property
    def fraction(self) -> float:
        return self.done / self.total_samples if self.total_samples else 0.0

    @property
    def sample_seconds(self) -> int:
        """Position in the recording of the sample in flight."""
        return max(0, self.index - 1) * SAMPLE_SECONDS

    @property
    def current(self) -> dict | None:
        """The most recent *identified* track, i.e. what the log last reported."""
        for result in reversed(self.results):
            if result:
                return result
        return None

    @property
    def unique_tracks(self) -> int:
        """Distinct songs seen so far.

        A rough live preview, not the final count: `deduplicate_tracklist()`
        also fuzzy-clusters remix/feat drift, smooths outliers and drops
        low-confidence singletons, so the tracklist that gets written is
        usually shorter than this.
        """
        return len({(r["artist"], r["title"]) for r in self.results if r})

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    @property
    def phase_remaining(self) -> float:
        if self.phase_deadline is None:
            return 0.0
        return max(0.0, self.phase_deadline - self.clock())

    @property
    def tick(self) -> int:
        """Animation frame, derived from the clock so `Live`'s own refresh thread
        advances the spinner without the pipeline ticking anything."""
        return int(self.elapsed * _SPIN_HZ)

    @property
    def samples_this_run(self) -> int:
        """Samples actually fetched now -- a resume's cached results cost no time."""
        return max(0, self.done - self.resumed_from)

    @property
    def seconds_per_sample(self) -> float:
        if self.samples_this_run >= 2 and self.elapsed > 0:
            return self.elapsed / self.samples_this_run
        return self.delay_seconds + 3.0  # nominal: the delay plus a typical call

    @property
    def eta_seconds(self) -> float:
        return max(0, self.total_samples - self.done) * self.seconds_per_sample


# --------------------------------------------------------------------------
# Rendering. Every glyph below is deliberately one cell wide (no ⏳/⌛): inside a
# box, a glyph rich measures as 2 and the terminal draws as 1 bends the right
# border on every redraw.
# --------------------------------------------------------------------------
def _bar(state: RunState, width: int) -> Text:
    """Progress bar in eighths, with any resumed-from head shown as spent elsewhere."""
    total = state.total_samples or 1
    eighths = min(int(round(state.fraction * width * 8)), width * 8)
    full, remainder = divmod(eighths, 8)
    carried = min(int(state.resumed_from / total * width), full)

    bar = Text(no_wrap=True)
    if carried:
        # Carried in from a resumed run: spent, but not earned this hour. A
        # lighter *texture* rather than a lighter grey, so it survives monochrome.
        bar.append("▓" * carried, style=_ACCENT)
    bar.append("█" * (full - carried), style=_ACCENT)
    used = full
    if remainder and full < width:
        bar.append(_PARTIAL[remainder], style=_ACCENT)
        used += 1
    bar.append("░" * (width - used), style=_TRACK)
    return bar


def _meter(value: float, cells: int = METER) -> Text:
    """A heavy/light rule -- distinct from the block progress bar, and still
    readable with no colour at all."""
    filled = max(0, min(cells, int(round(value * cells))))
    return Text.assemble(("━" * filled, _ACCENT), ("─" * (cells - filled), _TRACK))


def _one_line(value: object) -> str:
    """Collapse every run of whitespace, so no field can add a row to the panel.

    A newline inside a Shazam title is enough to make the box seven rows instead
    of six, and `Live` erases a fixed number of lines -- a panel whose height
    changes corrupts the redraw, which is the one failure this layout must not
    have. `Text(no_wrap=True)` does not help: the break is in the string.
    """
    return " ".join(str(value).split())


def _row(left: Text, right: Text, inner: int, rail: int = RAIL) -> Text:
    """One content line: left column truncated, right rail right-justified.

    The rail is a fixed width so the four numbers that get re-read most sit in
    the same cells every frame.
    """
    left_width = max(8, inner - rail - GAP)
    left.truncate(left_width, overflow="ellipsis")
    left.pad_right(max(0, left_width - left.cell_len))
    right.truncate(rail, overflow="ellipsis")
    right.pad_left(max(0, rail - right.cell_len))

    line = Text(no_wrap=True, overflow="ellipsis")
    line.append_text(left)
    line.append(" " * GAP)
    line.append_text(right)
    return line


def _position(state: RunState) -> Text:
    """Where in the recording we are; a finished run reads as fully covered."""
    at = state.audio_seconds if state.phase == "done" else state.sample_seconds
    return Text(f"{format_timestamp(at)} / {format_timestamp(state.audio_seconds)}", style=_MUTED)


def _track_row(state: RunState) -> tuple[Text, Text]:
    track = state.current
    if not track:
        return (
            Text("♪ no match yet", style=_TRACK),
            Text("—", style=_TRACK),
        )
    left = Text("♪ ", style=_MUTED)
    left.append(_one_line(track.get("artist") or "unknown artist"))
    left.append(" — ", style=_TRACK)
    left.append(_one_line(track.get("title") or "unknown title"), style=_MUTED)

    confidence = track.get("confidence")
    if confidence is None:
        return left, Text("--%", style=_TRACK)
    right = Text(f"{round(confidence * 100):>3d}% ", style=_MUTED)
    right.append_text(_meter(confidence))
    return left, right


def _phase_row(state: RunState) -> Text:
    """Whole seconds, not tenths: this redraws several times a second for an hour."""
    remaining = int(state.phase_remaining + 0.5)
    if state.phase == "cooldown":
        return Text(f"⧗ pausing between calls · next sample in {remaining}s", style=_MUTED)
    if state.phase == "backoff":
        if remaining > 0:
            return Text(
                f"⚠ rate limited · retry {state.retry}/{state.max_retries} in {remaining}s",
                style="yellow",
            )
        # The wait is over and the retry request is in flight. The phase stays
        # "backoff" until the next sample begins, so without this the panel
        # would sit on "in 0s" for the whole retry.
        spinner = _SPIN[state.tick % len(_SPIN)]
        return Text(
            f"{spinner} retrying sample {max(1, state.index)} after rate limit…",
            style="yellow",
        )
    if state.phase == "done":
        return Text(f"✓ finished all {state.total_samples} samples", style=_ACCENT)
    spinner = _SPIN[state.tick % len(_SPIN)]
    # `Live` renders once before the loop calls begin_sample(), so clamp rather
    # than announce "sample 0" for that first frame.
    return Text(f"{spinner} asking Shazam about sample {max(1, state.index)}…", style=_ACCENT)


def _stats_row(state: RunState) -> tuple[Text, Text]:
    """The payoff line: what you have, how good it is, how long it still owes you."""
    if state.done:
        tracks = state.unique_tracks
        left = Text(f"{tracks} track{'' if tracks == 1 else 's'}", style=_MUTED)
        left.append(" · ", style=_TRACK)
        left.append(
            f"{state.hits} of {state.done} samples matched ({state.hit_rate * 100:.0f}%)",
            style=_MUTED,
        )
    else:
        left = Text("no samples finished yet", style=_TRACK)

    if state.phase == "done":
        # Not an ETA any more: what a re-run of this file would cost.
        right = Text("", style=_TRACK)
        right.append(f"{state.seconds_per_sample:.1f}s/sample", style=_MUTED)
    else:
        rough = "" if state.samples_this_run >= SETTLED_SAMPLES else "~"
        right = Text("ETA ", style=_TRACK)
        right.append(f"{rough}{format_duration(state.eta_seconds)}", style=_MUTED)
    return left, right


def _title(state: RunState, box_width: int) -> Text:
    word, _, title_style = _FRAME.get(state.phase, _FRAME["identifying"])
    title = Text(word, style=title_style)
    name = Text(_one_line(state.source_name), style=_MUTED)
    # Corners, the rule either side of the title, and its spaces: keep well clear.
    name.truncate(max(8, min(64, box_width - 10 - len(word) - 3)), overflow="ellipsis")
    title.append(" · ", style=_TRACK)
    title.append_text(name)
    return title


def render_panel(state: RunState, width: int) -> Group:
    """Render the panel at `width` columns. Pure: same state and clock, same output."""
    box_width = max(MIN_BOX, min(width, MAX_BOX))
    inner = box_width - 4  # two borders plus a column of padding each side

    # The position cell is the widest thing in the rail, and a 10-hour recording
    # needs more than "1:42:30 / 1:42:30" -- widen rather than ellipsize the one
    # number the rail exists to hold steady. Never past half the inner width.
    position = _position(state)
    rail = max(RAIL, min(position.cell_len, inner // 2))
    left_width = max(8, inner - rail - GAP)

    percent = f"{state.fraction * 100:.0f}%"
    counter = Text(
        f"  {state.done:>{len(str(state.total_samples))}}/{state.total_samples}  {percent:>4}",
        style=_MUTED,
    )
    # The bar takes whatever the counter does not: a capped bar leaves a hole
    # mid-row on a wide terminal, and every other row fills its column.
    head = _bar(state, max(6, left_width - counter.cell_len))
    head.append_text(counter)

    track_left, track_right = _track_row(state)
    stats_left, stats_right = _stats_row(state)
    elapsed = Text("elapsed ", style=_TRACK)
    elapsed.append(format_duration(state.elapsed), style=_MUTED)

    body = Group(
        _row(head, position, inner, rail),
        _row(track_left, track_right, inner, rail),
        _row(_phase_row(state), elapsed, inner, rail),
        _row(stats_left, stats_right, inner, rail),
    )
    return Group(
        Text(""),
        Panel(
            body,
            title=_title(state, box_width),
            title_align="left",
            border_style=_FRAME.get(state.phase, _FRAME["identifying"])[1],
            box=ROUNDED,
            width=box_width,
            padding=(0, 1),
        ),
    )


class ProgressPanel:
    """A self-refreshing renderable wrapper around `render_panel()`.

    `Live` holds this object and re-renders it on its own thread; because
    `__rich_console__` recomputes from `state` (whose moving parts read the
    clock), the countdown animates during an untouched `await asyncio.sleep()`.
    """

    def __init__(self, state, render=render_panel):
        self.state = state
        self._render = render

    def __rich_console__(self, console, options):
        yield self._render(self.state, console.width)


REFRESH_PER_SECOND = 8


class _PlainDisplay:
    """The no-panel path: log lines go straight to stdout, exactly as before."""

    def log(self, text: str) -> None:
        print(text)


class _LiveDisplay:
    """The panel path: log lines scroll *above* the pinned panel.

    `Text.from_ansi` is required rather than a bare `console.print(str)` --
    `format_progress_line()`'s output contains both ANSI colour and square
    brackets, and rich would otherwise read `[41/205]` as console markup.
    """

    def __init__(self, live):
        self._live = live

    def log(self, text: str) -> None:
        self._live.console.print(Text.from_ansi(text))


@contextmanager
def live_display(state, enabled: bool, render=render_panel):
    """Yield a display that logs above a pinned panel, or plain stdout if disabled.

    Keeping both paths behind one object is what lets the identify loop stay a
    single code path: there is no `if live:` inside it.
    """
    if not enabled:
        yield _PlainDisplay()
        return

    console = Console()
    with Live(
        ProgressPanel(state, render),
        console=console,
        refresh_per_second=REFRESH_PER_SECOND,
        transient=False,
        # stdout redirection is load-bearing -- it is what puts the raw print()s
        # still in shazam_client above the panel instead of through it. stderr
        # must be left alone: rich redirects it whenever *stdout* is a terminal,
        # so `identify set.mp3 2> errors.log` would otherwise capture nothing
        # and dump every warning onto the terminal instead.
        redirect_stderr=False,
    ) as live:
        yield _LiveDisplay(live)


# --------------------------------------------------------------------------
# The adaptive run's panel. `AdaptiveRunState` is a deliberate small twin of
# `RunState` rather than a shared base: `RunState` has non-default fields, so a
# default-bearing mixin cannot slot under it without making the whole
# sequential path keyword-only. The clock discipline is identical -- `clock` is
# injected and `started_at` reads from it -- and so is the fixed-height rule.
# --------------------------------------------------------------------------
@dataclass
class AdaptiveRunState:
    """Everything the adaptive panel draws.

    The counters the sequential panel derives from a fixed sample list are
    replaced by ones the engine reports (`update_from_engine`): there is no
    total to count towards, only remaining uncertainty to shrink.
    """

    source_name: str
    audio_seconds: int
    delay_seconds: int
    resumed_from: int = 0
    probes_done: int = 0
    hits: int = 0
    tracks_found: int = 0
    boundaries_found: int = 0
    boundaries_at_target: int = 0
    widest_gap: float = 0.0  # widest active boundary interval, seconds
    est_probes_remaining: int = 0
    current_t: float = 0.0  # position of the probe in flight
    current_purpose: str = "coverage"
    current_result: dict | None = None
    phase: str = "identifying"  # identifying | cooldown | backoff | done
    phase_deadline: float | None = None
    retry: int = 0
    max_retries: int = MAX_RETRIES
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    # ---- transitions, called from the driver -----------------------------
    def begin_probe(self, plan) -> None:
        self.current_t = plan.t
        self.current_purpose = plan.purpose
        self.phase = "identifying"
        self.phase_deadline = None
        self.retry = 0

    def record(self, track_info: dict | None) -> None:
        self.probes_done += 1
        if track_info:
            self.hits += 1
            self.current_result = track_info

    def update_from_engine(self, engine) -> None:
        segments, _drops = engine.segments()
        self.tracks_found = sum(1 for s in segments if s.info)
        self.boundaries_found, self.boundaries_at_target = engine.boundary_stats()
        self.widest_gap = engine.max_boundary_width
        self.est_probes_remaining = engine.estimated_probes_remaining()

    def begin_cooldown(self, seconds: float) -> None:
        self.phase = "cooldown"
        self.phase_deadline = self.clock() + seconds

    def begin_backoff(self, seconds: float, attempt: int) -> None:
        self.phase = "backoff"
        self.phase_deadline = self.clock() + seconds
        self.retry = attempt

    def finish(self) -> None:
        self.phase = "done"
        self.phase_deadline = None

    # ---- derived ---------------------------------------------------------
    @property
    def fraction(self) -> float:
        """Share of the *expected* work done. The denominator moves as the
        engine learns, which is honest: there is no fixed total."""
        if self.phase == "done":
            return 1.0
        total = self.probes_done + self.est_probes_remaining
        return self.probes_done / total if total else 0.0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.probes_done if self.probes_done else 0.0

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    @property
    def phase_remaining(self) -> float:
        if self.phase_deadline is None:
            return 0.0
        return max(0.0, self.phase_deadline - self.clock())

    @property
    def tick(self) -> int:
        return int(self.elapsed * _SPIN_HZ)

    @property
    def probes_this_run(self) -> int:
        return max(0, self.probes_done - self.resumed_from)

    @property
    def seconds_per_probe(self) -> float:
        if self.probes_this_run >= 2 and self.elapsed > 0:
            return self.elapsed / self.probes_this_run
        return self.delay_seconds + 3.0

    @property
    def eta_seconds(self) -> float:
        return self.est_probes_remaining * self.seconds_per_probe


def _adaptive_position(state: AdaptiveRunState) -> Text:
    at = state.audio_seconds if state.phase == "done" else int(state.current_t)
    return Text(f"{format_timestamp(at)} / {format_timestamp(state.audio_seconds)}", style=_MUTED)


def _adaptive_track_row(state: AdaptiveRunState) -> tuple[Text, Text]:
    track = state.current_result
    if not track:
        return Text("♪ no match yet", style=_TRACK), Text("—", style=_TRACK)
    left = Text("♪ ", style=_MUTED)
    left.append(_one_line(track.get("artist") or "unknown artist"))
    left.append(" — ", style=_TRACK)
    left.append(_one_line(track.get("title") or "unknown title"), style=_MUTED)

    confidence = track.get("confidence")
    if confidence is None:
        return left, Text("--%", style=_TRACK)
    right = Text(f"{round(confidence * 100):>3d}% ", style=_MUTED)
    right.append_text(_meter(confidence))
    return left, right


def _adaptive_phase_row(state: AdaptiveRunState) -> Text:
    remaining = int(state.phase_remaining + 0.5)
    if state.phase == "cooldown":
        return Text(f"⧗ pausing between calls · next probe in {remaining}s", style=_MUTED)
    if state.phase == "backoff":
        if remaining > 0:
            return Text(
                f"⚠ rate limited · retry {state.retry}/{state.max_retries} in {remaining}s",
                style="yellow",
            )
        spinner = _SPIN[state.tick % len(_SPIN)]
        return Text(f"{spinner} retrying after rate limit…", style="yellow")
    if state.phase == "done":
        return Text(f"✓ finished after {state.probes_done} probes", style=_ACCENT)
    spinner = _SPIN[state.tick % len(_SPIN)]
    where = format_timestamp(int(state.current_t))
    return Text(f"{spinner} probing {where} ({state.current_purpose})…", style=_ACCENT)


def _adaptive_stats_row(state: AdaptiveRunState) -> tuple[Text, Text]:
    """What you have, how sharp it is, how long it still owes you."""
    if state.probes_done:
        tracks = state.tracks_found
        left = Text(f"{tracks} track{'' if tracks == 1 else 's'}", style=_MUTED)
        left.append(" · ", style=_TRACK)
        left.append(
            f"{state.boundaries_at_target}/{state.boundaries_found} boundaries sharp",
            style=_MUTED,
        )
        if state.widest_gap > 0:
            left.append(" · ", style=_TRACK)
            left.append(f"widest ±{state.widest_gap / 2:.0f}s", style=_MUTED)
    else:
        left = Text("no probes finished yet", style=_TRACK)

    if state.phase == "done":
        right = Text("", style=_TRACK)
        right.append(f"{state.seconds_per_probe:.1f}s/probe", style=_MUTED)
    else:
        rough = "" if state.probes_this_run >= SETTLED_SAMPLES else "~"
        right = Text("ETA ", style=_TRACK)
        right.append(f"{rough}{format_duration(state.eta_seconds)}", style=_MUTED)
    return left, right


def _adaptive_title(state: AdaptiveRunState, box_width: int) -> Text:
    word, _, title_style = _FRAME.get(state.phase, _FRAME["identifying"])
    title = Text(word, style=title_style)
    name = Text(_one_line(state.source_name), style=_MUTED)
    name.truncate(max(8, min(64, box_width - 10 - len(word) - 3)), overflow="ellipsis")
    title.append(" · ", style=_TRACK)
    title.append_text(name)
    return title


def render_adaptive_panel(state: AdaptiveRunState, width: int) -> Group:
    """Render the adaptive panel at `width` columns. Pure, like `render_panel`."""
    box_width = max(MIN_BOX, min(width, MAX_BOX))
    inner = box_width - 4

    position = _adaptive_position(state)
    rail = max(RAIL, min(position.cell_len, inner // 2))
    left_width = max(8, inner - rail - GAP)

    percent = f"{state.fraction * 100:.0f}%"
    remaining = "" if state.phase == "done" else f"+{state.est_probes_remaining}"
    counter = Text(f"  {state.probes_done}{remaining}  {percent:>4}", style=_MUTED)
    head = _adaptive_bar(state, max(6, left_width - counter.cell_len))
    head.append_text(counter)

    track_left, track_right = _adaptive_track_row(state)
    stats_left, stats_right = _adaptive_stats_row(state)
    elapsed = Text("elapsed ", style=_TRACK)
    elapsed.append(format_duration(state.elapsed), style=_MUTED)

    body = Group(
        _row(head, position, inner, rail),
        _row(track_left, track_right, inner, rail),
        _row(_adaptive_phase_row(state), elapsed, inner, rail),
        _row(stats_left, stats_right, inner, rail),
    )
    return Group(
        Text(""),
        Panel(
            body,
            title=_adaptive_title(state, box_width),
            title_align="left",
            border_style=_FRAME.get(state.phase, _FRAME["identifying"])[1],
            box=ROUNDED,
            width=box_width,
            padding=(0, 1),
        ),
    )


def _adaptive_bar(state: AdaptiveRunState, width: int) -> Text:
    """Same eighths bar as the sequential panel, over a moving denominator."""
    eighths = min(int(round(state.fraction * width * 8)), width * 8)
    full, remainder = divmod(eighths, 8)
    carried = 0
    if state.probes_done:
        carried = min(int(state.resumed_from / state.probes_done * full), full)

    bar = Text(no_wrap=True)
    if carried:
        bar.append("▓" * carried, style=_ACCENT)
    bar.append("█" * (full - carried), style=_ACCENT)
    used = full
    if remainder and full < width:
        bar.append(_PARTIAL[remainder], style=_ACCENT)
        used += 1
    bar.append("░" * (width - used), style=_TRACK)
    return bar
