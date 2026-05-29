"""Pilot-driven tests for opening the edit dialog from the tracklist.

Regression coverage for the Enter key: the focused DataTable binds Enter to its
own ``select_cursor`` action, which would otherwise shadow the editor's
``edit_track`` binding and leave Enter doing nothing. The editor handles
``DataTable.RowSelected`` (posted by that built-in action) to open the modal.

The repo has no pytest-asyncio, so each test wraps an async pilot routine in
asyncio.run(), matching test_editor_playback.py.
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


def test_enter_opens_edit_dialog(tmp_path):
    """Pressing Enter on the table opens the EditTrackScreen modal."""

    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditTrackScreen)

    asyncio.run(routine(tmp_path))


def test_enter_edit_roundtrip_updates_track(tmp_path):
    """Editing via Enter and saving writes the new values back to the track."""

    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # open modal on row 0
            await pilot.pause()
            modal = app.screen  # the EditTrackScreen on top of the stack
            modal.query_one("#artist-input", Input).value = "New Artist"
            modal.query_one("#title-input", Input).value = "New Title"
            await pilot.press("enter")  # artist-input -> focus title-input
            await pilot.pause()
            await pilot.press("enter")  # title-input -> save + dismiss
            await pilot.pause()
            assert not isinstance(app.screen, EditTrackScreen)
            assert app.tracklist.tracks[0].artist == "New Artist"
            assert app.tracklist.tracks[0].title == "New Title"
            assert app.unsaved_changes

    asyncio.run(routine(tmp_path))
