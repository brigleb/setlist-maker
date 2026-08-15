"""Pilot-driven tests for opening the edit dialog from the tracklist.

Regression coverage for the Enter key: the focused DataTable binds Enter to its
own ``select_cursor`` action, which would otherwise shadow the editor's
``edit_track`` binding and leave Enter doing nothing. The editor handles
``DataTable.RowSelected`` (posted by that built-in action) to open the modal.

The repo has no pytest-asyncio, so each test wraps an async pilot routine in
asyncio.run(), matching test_editor_playback.py.

Steps wait on the state they depend on (the shared ``wait_until`` fixture in
conftest.py) rather than assuming a ``pilot.pause()`` has let focus settle: on a
loaded machine it may not have, and the next Enter then re-submits the artist
field instead of saving (#35).
"""

import asyncio

from textual.widgets import Input

from setlist_maker.editor import EditTrackScreen, Track, Tracklist, TracklistEditor


def _build_app(tmp_path):
    md = tmp_path / "set_tracklist.md"
    tracklist = Tracklist(
        source_file="set.mp3",
        tracks=[
            Track(timestamp=0, artist="A", title="One"),
            Track(timestamp=300, artist="B", title="Two"),
        ],
    )
    return TracklistEditor(tracklist, md, corrections_db=None)


def _modal_open_with_artist_focused(app):
    """The edit modal is on the stack and its artist field has taken focus."""
    focused_id = getattr(app.focused, "id", None)
    return isinstance(app.screen, EditTrackScreen) and focused_id == "artist-input"


def test_enter_opens_edit_dialog(tmp_path, wait_until):
    """Pressing Enter on the table opens the EditTrackScreen modal."""

    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await wait_until(
                pilot, lambda: _modal_open_with_artist_focused(app), what="edit modal to open"
            )

    asyncio.run(routine(tmp_path))


def test_enter_edit_roundtrip_updates_track(tmp_path, wait_until):
    """Editing via Enter and saving writes the new values back to the track."""

    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # open modal on row 0
            await wait_until(
                pilot, lambda: _modal_open_with_artist_focused(app), what="edit modal to open"
            )
            modal = app.screen  # the EditTrackScreen on top of the stack
            title_input = modal.query_one("#title-input", Input)
            modal.query_one("#artist-input", Input).value = "New Artist"
            title_input.value = "New Title"
            await pilot.press("enter")  # artist-input -> focus title-input
            # Submitted -> focus() -> deferred set_focus: the hop a loaded machine can
            # leave unfinished, sending the next Enter back to the artist field.
            await wait_until(
                pilot, lambda: app.focused is title_input, what="focus to move to the title field"
            )
            await pilot.press("enter")  # title-input -> save + dismiss
            await wait_until(
                pilot,
                lambda: not isinstance(app.screen, EditTrackScreen),
                what="edit modal to dismiss",
            )
            assert app.tracklist.tracks[0].artist == "New Artist"
            assert app.tracklist.tracks[0].title == "New Title"
            assert app.unsaved_changes

    asyncio.run(routine(tmp_path))
