"""Pilot-driven tests for the editor's playback wiring.

These exercise the Textual UI with a fake PlaybackController (no real ffplay is
spawned). The repo has no pytest-asyncio, so each test wraps an async pilot
routine in asyncio.run(), matching how the app is driven at runtime.
"""

import asyncio
from unittest.mock import patch

from setlist_maker.editor import Track, Tracklist, TracklistEditor


class FakeController:
    """Records play/stop calls and simulates is_playing without a subprocess."""

    def __init__(self) -> None:
        self.playing = False
        self.calls: list[tuple] = []

    def play(self, audio_path, start_seconds, duration=30) -> None:
        self.calls.append(("play", str(audio_path), start_seconds))
        self.playing = True

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.playing = False

    def is_playing(self) -> bool:
        return self.playing

    def elapsed(self) -> float:
        return 2.0

    @property
    def play_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "play"]


def _build_app(tmp_path):
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"dummy")
    md = tmp_path / "set_tracklist.md"
    tracklist = Tracklist(
        source_file="set.mp3",
        tracks=[
            Track(timestamp=0, artist="A", title="One"),
            Track(timestamp=300, artist="", title=""),  # unidentified -> previewable
        ],
    )
    return TracklistEditor(tracklist, md, corrections_db=None, audio_path=audio)


def _run(tmp_path, routine, *, enabled=True):
    """Run an async pilot routine with playback mocked and capability forced."""
    with (
        patch("setlist_maker.editor.PlaybackController", FakeController),
        patch("setlist_maker.editor.playback_available", return_value=enabled),
    ):
        asyncio.run(routine(tmp_path))


def test_p_starts_preview_with_track_timestamp(tmp_path):
    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.playback_enabled = True
            await pilot.press("p")
            await pilot.pause()
            assert app.playback.is_playing()
            assert app._playing_row == 0
            assert app.playback.play_calls == [("play", str(app.audio_path), 0)]

    _run(tmp_path, routine)


def test_p_again_on_same_row_stops(tmp_path):
    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.playback_enabled = True
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert not app.playback.is_playing()
            assert app._playing_row is None
            assert ("stop",) in app.playback.calls

    _run(tmp_path, routine)


def test_navigation_stops_preview(tmp_path):
    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.playback_enabled = True
            await pilot.press("p")
            await pilot.pause()
            assert app.playback.is_playing()
            await pilot.press("down")
            await pilot.pause()
            assert not app.playback.is_playing()
            assert app._playing_row is None

    _run(tmp_path, routine)


def test_reject_stops_preview_on_row_zero(tmp_path):
    # Regression: rejecting the playing track on row 0 must stop audio, not
    # rely on an incidental cursor change that never fires for row 0.
    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.playback_enabled = True
            await pilot.press("p")  # play row 0
            await pilot.pause()
            assert app.playback.is_playing()
            await pilot.press("space")  # reject row 0
            await pilot.pause()
            assert not app.playback.is_playing()
            assert app._playing_row is None

    _run(tmp_path, routine)


def test_unmount_stops_preview(tmp_path):
    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.playback_enabled = True
            await pilot.press("p")
            await pilot.pause()
            assert app.playback.is_playing()
        # After the app exits, on_unmount must have stopped playback.
        assert not app.playback.is_playing()
        assert ("stop",) in app.playback.calls

    _run(tmp_path, routine)


def test_disabled_playback_does_not_spawn(tmp_path):
    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.playback_enabled = False
            await pilot.press("p")
            await pilot.pause()
            assert app.playback.play_calls == []
            assert not app.playback.is_playing()

    _run(tmp_path, routine, enabled=False)


def test_unidentified_row_is_previewable(tmp_path):
    async def routine(tmp_path):
        app = _build_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.playback_enabled = True
            await pilot.press("down")  # move to the unidentified row 1
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert app.playback.is_playing()
            assert app._playing_row == 1
            assert app.playback.play_calls == [("play", str(app.audio_path), 300)]

    _run(tmp_path, routine)
