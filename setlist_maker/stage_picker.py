"""
Interactive stage picker for the audio processing pipeline.

Presents a checkbox list of processing stages using Textual,
allowing the user to toggle stages on/off before proceeding.
"""

from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option


@dataclass
class Stage:
    """A processing stage that can be toggled on/off."""

    key: str
    label: str
    enabled: bool = True


class StagePickerApp(App[list[str] | None]):
    """Interactive picker for selecting processing stages."""

    CSS = """
    Screen {
        align: center middle;
    }

    #picker-container {
        width: 56;
        height: auto;
        max-height: 20;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }

    #title {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #stage-list {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }

    #hint {
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("enter", "confirm", "Proceed", priority=True),
        Binding("escape,q", "cancel", "Cancel", priority=True),
        Binding("space", "toggle_stage", "Toggle", show=False, priority=True),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("a", "select_all", "All", priority=True),
        Binding("n", "select_none", "None", priority=True),
    ]

    def __init__(self, stages: list[Stage]) -> None:
        super().__init__()
        self.stages = stages

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-container"):
            yield Label("Processing Stages", id="title")
            yield OptionList(
                *[self._render_option(s) for s in self.stages],
                id="stage-list",
            )
            yield Static(
                "[dim]space[/] toggle  [dim]a[/] all  [dim]n[/] none  "
                "[dim]enter[/] proceed  [dim]q[/] cancel",
                id="hint",
            )

    def _render_option(self, stage: Stage) -> Option:
        check = "[bold green]✓[/]" if stage.enabled else "[dim]·[/]"
        return Option(f" {check}  {stage.label}", id=stage.key)

    def _refresh_list(self) -> None:
        option_list = self.query_one("#stage-list", OptionList)
        highlighted = option_list.highlighted
        option_list.clear_options()
        for stage in self.stages:
            option_list.add_option(self._render_option(stage))
        if highlighted is not None:
            option_list.highlighted = highlighted

    def action_toggle_stage(self) -> None:
        option_list = self.query_one("#stage-list", OptionList)
        idx = option_list.highlighted
        if idx is not None and 0 <= idx < len(self.stages):
            self.stages[idx].enabled = not self.stages[idx].enabled
            self._refresh_list()

    def action_select_all(self) -> None:
        for stage in self.stages:
            stage.enabled = True
        self._refresh_list()

    def action_select_none(self) -> None:
        for stage in self.stages:
            stage.enabled = False
        self._refresh_list()

    def action_confirm(self) -> None:
        selected = [s.key for s in self.stages if s.enabled]
        self.exit(selected)

    def action_cancel(self) -> None:
        self.exit(None)

    def action_cursor_down(self) -> None:
        self.query_one("#stage-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#stage-list", OptionList).action_cursor_up()


def run_stage_picker(stages: list[Stage]) -> list[str] | None:
    """
    Launch the interactive stage picker.

    Args:
        stages: List of Stage objects to display.

    Returns:
        List of selected stage keys, or None if the user cancelled.
    """
    app = StagePickerApp(stages)
    return app.run()
