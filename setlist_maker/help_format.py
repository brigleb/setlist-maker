"""Colorized `--help` output for the CLI.

`setlist-maker` with no arguments prints a hand-written epilog that *is* the
manual — the workflow, every command, every flag and a worked example block.
In one flat colour that is a wall of text you have to read linearly; painted,
the headings, the flags and the placeholders separate at a glance.

Deliberately a **post-pass over argparse's own output**, not a rewrite of the
help text in markup. The epilog stays the single source of truth (it already
sources every default from its canonical constant, and duplicating it in
markup would mean maintaining the manual twice), and every screen argparse can
produce — top level, `identify -h`, `chapters -h`, whatever is added later —
is coloured by the same rules with no per-screen upkeep. The cost is that the
rules are regexes over rendered text rather than semantic knowledge, so each
one below is anchored on something argparse's formatter actually guarantees
(line-start headings, `--flag METAVAR`) rather than on prose.

The palette is `progress.py`'s, so the help screen and the run panel read as
one program: `_ACCENT` green for the things you type, `_MUTED` grey for the
things you substitute, bold for structure.

Colour is decided by rich's `Console`, not by an `isatty()` check of our own,
which is what gets `NO_COLOR`, `TERM=dumb` and redirection handled the same
way here as everywhere else rich is used. Piped output is byte-identical to
the plain `format_help()` it was built from — `soft_wrap` is on because the
epilog's columns are hand-aligned past the 80-column default and rich would
otherwise re-wrap them into a mess.
"""

import argparse
import re
import sys
from typing import IO, Sequence

from rich.console import Console
from rich.text import Text
from rich.theme import Theme

# Mirrors progress.py so the help and the live panel share one palette.
_ACCENT = "green"
_MUTED = "grey62"

HELP_THEME = Theme(
    {
        "help.heading": "bold",
        "help.command": f"bold {_ACCENT}",
        "help.flag": _ACCENT,
        "help.metavar": _MUTED,
        "help.placeholder": _MUTED,
        "help.default": _MUTED,
        "help.usage": _MUTED,
    }
)

# A section heading is a short, punctuation-free line sitting at (or near) the
# left margin: argparse's own "options:" and the epilog's "Typical workflow",
# "identify options", the indented "  detection tuning". The length cap and the
# letters-only class are what keep prose out — every body line in the epilog is
# either indented further, longer than 29 characters, or contains punctuation.
_HEADING = re.compile(r"^ {0,2}(?P<heading>[A-Za-z][A-Za-z ]{0,28}:?)$", re.MULTILINE)

# `-h`, `--web-edit`. The lookbehind is what keeps "0-1", "single-sample" and
# "A B A -> A" out of the flag colour.
_FLAG = re.compile(r"(?<![\w-])(?P<flag>--?[A-Za-z][\w-]*)")

# Only an uppercase token *immediately following a flag* is a metavar; matching
# bare uppercase words would paint "DJ", "MP3" and "JSON" out of the prose.
_METAVAR = re.compile(r"--[\w-]+[ =](?P<metavar>[A-Z][A-Z0-9_]*)\b")

_PLACEHOLDER = re.compile(r"(?P<placeholder><[^<>\n]+>)")
_DEFAULT = re.compile(r"(?P<default>\(default: [^)\n]*\))")
_USAGE = re.compile(r"^(?P<usage>usage:)", re.MULTILINE)


def colorize_help(help_text: str, *, commands: Sequence[str] = (), prog: str = "") -> Text:
    """Return `help_text` as a styled `Text`, unchanged character for character.

    Pure: no console, no terminal, no I/O — so the rules can be unit-tested by
    reading spans back off the result, the way `progress.render_panel()` is.
    """
    text = Text(help_text)
    text.highlight_regex(_USAGE, style_prefix="help.")
    text.highlight_regex(_DEFAULT, style_prefix="help.")
    text.highlight_regex(_PLACEHOLDER, style_prefix="help.")
    text.highlight_regex(_FLAG, style_prefix="help.")
    text.highlight_regex(_METAVAR, style_prefix="help.")

    # Subcommand names are highlighted only where they are unambiguously being
    # named as commands — at the start of a listing row, or straight after the
    # program name in an example. Every other "identify" in the epilog is prose.
    for name in commands:
        word = re.escape(name)
        text.highlight_regex(
            re.compile(rf"^ {{1,6}}(?P<command>{word})(?=\s|$)", re.MULTILINE), style_prefix="help."
        )
        if prog:
            text.highlight_regex(
                re.compile(rf"(?<={re.escape(prog)} )(?P<command>{word})(?=\s|$)"),
                style_prefix="help.",
            )

    text.highlight_regex(_HEADING, style_prefix="help.")
    return text


class ColorHelpParser(argparse.ArgumentParser):
    """`ArgumentParser` that prints its help colourized on a terminal.

    Overriding `print_help` catches both routes into it: the explicit call in
    `main()` for the no-argument screen, and argparse's own `-h` action. It
    also inherits for free — `add_subparsers()` builds children out of
    `type(self)` — so `identify -h` and `chapters -h` come along without
    wiring, which is why the constructor signature must stay untouched.
    """

    def add_subparsers(self, **kwargs):
        action = super().add_subparsers(**kwargs)
        # `choices` is the live dict `add_parser()` fills in, so reading it at
        # print time means a new subcommand is highlighted without being listed
        # here. Only the root parser has one; subcommand screens don't name
        # sibling commands, so there is nothing for them to inherit.
        self._subcommand_action = action
        return action

    def print_help(self, file: IO[str] | None = None) -> None:
        stream = file or sys.stdout
        action = getattr(self, "_subcommand_action", None)
        console = Console(
            file=stream,
            theme=HELP_THEME,
            soft_wrap=True,
            highlight=False,
        )
        console.print(
            colorize_help(
                self.format_help(),
                commands=tuple(action.choices) if action is not None else (),
                prog=(self.prog or "").split(" ")[0],
            ),
            end="",
        )
