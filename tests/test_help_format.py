"""Tests for setlist_maker.help_format — colorized CLI help.

The invariant that matters most is that colour is *only* colour: the styled
help must be the same characters as argparse's own, so a piped or redirected
`--help` is unchanged and no rule can ever eat a flag off the screen.
"""

import io
import re
import sys

import pytest
from rich.console import Console

from setlist_maker.cli import main
from setlist_maker.help_format import HELP_THEME, ColorHelpParser, colorize_help

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _build_parser(argv=("setlist-maker",)):
    """Return the CLI's real parser by capturing the help it prints with no args."""
    captured = {}
    original = ColorHelpParser.print_help

    def spy(self, file=None):
        captured.setdefault("parser", self)
        original(self, file)

    ColorHelpParser.print_help = spy
    buffer = io.StringIO()
    old_argv, old_stdout = sys.argv, sys.stdout
    sys.argv = list(argv)
    sys.stdout = buffer
    try:
        with pytest.raises(SystemExit):
            main()
    finally:
        ColorHelpParser.print_help = original
        sys.argv, sys.stdout = old_argv, old_stdout
    return captured["parser"], buffer


@pytest.fixture
def parser():
    return _build_parser()[0]


def _render(parser, *, color: bool) -> str:
    """Render the parser's help the way `print_help` does, forcing the tty question."""
    console = Console(
        file=io.StringIO(),
        theme=HELP_THEME,
        soft_wrap=True,
        highlight=False,
        force_terminal=color or None,
        no_color=not color,
        width=200,
    )
    action = getattr(parser, "_subcommand_action", None)
    console.print(
        colorize_help(
            parser.format_help(),
            commands=tuple(action.choices) if action is not None else (),
            prog=parser.prog.split(" ")[0],
        ),
        end="",
    )
    return console.file.getvalue()


def test_colorized_help_has_the_same_characters_as_plain_help(parser):
    """Styling must be purely additive — strip the escapes and nothing moved."""
    assert _ANSI.sub("", _render(parser, color=True)) == parser.format_help()


def test_help_is_plain_when_stdout_is_not_a_terminal(parser):
    """A piped or redirected `--help` stays byte-identical to argparse's output."""
    out = _render(parser, color=False)
    assert "\x1b" not in out
    assert out == parser.format_help()


def test_running_with_no_arguments_prints_the_full_help_unstyled_to_a_pipe():
    """The screen the user actually sees with no arguments, captured off a StringIO."""
    parser, stream = _build_parser()
    out = stream.getvalue()
    assert "\x1b" not in out
    assert out == parser.format_help()
    assert "Typical workflow" in out


def test_flags_and_headings_are_styled(parser):
    styled = colorize_help(
        parser.format_help(), commands=("identify", "chapters"), prog="setlist-maker"
    )
    spans = {(styled.plain[s.start : s.end], s.style) for s in styled.spans}
    assert ("--web-edit", "help.flag") in spans
    assert ("Typical workflow", "help.heading") in spans
    assert ("SECONDS", "help.metavar") in spans
    assert ("(default: 15)", "help.default") in spans
    assert ("<tracklist.md>", "help.placeholder") in spans


@pytest.mark.parametrize(
    "phrase",
    [
        "0-1",  # a range in "Title similarity 0-1 to merge matches"
        "1-sample",  # a compound in "keep a 1-sample track"
        "single-sample",  # a compound in the --no-smoothing description
        "-> A",  # the arrow in "(A B A -> A)"
    ],
)
def test_hyphenated_prose_is_not_mistaken_for_a_flag(parser, phrase):
    """The flag rule is anchored so ordinary hyphens in the help text stay plain."""
    styled = colorize_help(
        parser.format_help(), commands=("identify", "chapters"), prog="setlist-maker"
    )
    assert phrase in styled.plain
    flagged = {styled.plain[s.start : s.end] for s in styled.spans if s.style == "help.flag"}
    assert not any(f in phrase for f in flagged if f.strip("-"))


def test_prose_lines_are_not_mistaken_for_headings(parser):
    """Only real section headings go bold — the epilog's prose must stay unstyled."""
    styled = colorize_help(parser.format_help(), commands=(), prog="setlist-maker")
    headings = {
        styled.plain[s.start : s.end].strip() for s in styled.spans if s.style == "help.heading"
    }
    assert headings == {
        "positional arguments:",
        "options:",
        "Typical workflow",
        "Commands",
        "identify options",
        "adaptive sampling",
        "detection tuning",
        "chapters options",
        "global options",
        "Examples",
    }


def test_subcommand_help_inherits_the_colorizing_parser(parser):
    """`identify -h` / `chapters -h` come along for free via add_subparsers()."""
    for sub in parser._subcommand_action.choices.values():
        assert isinstance(sub, ColorHelpParser)
        assert _ANSI.sub("", _render(sub, color=True)) == sub.format_help()
