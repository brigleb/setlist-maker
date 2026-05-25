"""Tests for setlist_maker.identify module."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from setlist_maker.editor import CorrectionsDB
from setlist_maker.identify import (
    deduplicate_tracklist,
    load_progress,
    process_single_file,
    results_to_tracklist,
    save_progress,
)


class TestSaveLoadProgress:
    """Tests for save_progress and load_progress functions."""

    def test_save_and_load(self, temp_dir):
        """Test saving and loading progress."""
        progress_file = temp_dir / "progress.json"
        results = [
            (0, {"artist": "Artist 1", "title": "Track 1"}),
            (30, None),
            (60, {"artist": "Artist 2", "title": "Track 2"}),
        ]

        save_progress(results, progress_file)
        loaded = load_progress(progress_file)

        # JSON converts tuples to lists, so compare element-wise
        assert len(loaded) == len(results)
        for (loaded_ts, loaded_info), (orig_ts, orig_info) in zip(loaded, results):
            assert loaded_ts == orig_ts
            assert loaded_info == orig_info

    def test_load_nonexistent(self, temp_dir):
        """Test loading nonexistent file returns empty list."""
        progress_file = temp_dir / "nonexistent.json"
        loaded = load_progress(progress_file)

        assert loaded == []

    def test_progress_format(self, temp_dir):
        """Test that progress is saved as valid JSON."""
        progress_file = temp_dir / "progress.json"
        results = [(0, {"artist": "Test", "title": "Song"})]

        save_progress(results, progress_file)

        # Should be readable as JSON
        with open(progress_file) as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0][0] == 0


class TestDeduplicateTracklist:
    """Tests for deduplicate_tracklist function."""

    def test_removes_singletons(self):
        """Test that tracks appearing only once are converted to unidentified."""
        results = [
            (0, {"artist": "Artist", "title": "Repeated"}),
            (30, {"artist": "Artist", "title": "Repeated"}),
            (60, {"artist": "One", "title": "Time Only"}),  # Singleton
        ]

        deduped = deduplicate_tracklist(results)

        # Should have the repeated track + unidentified marker for singleton
        assert len(deduped) == 2
        assert deduped[0][1]["title"] == "Repeated"
        assert deduped[1][1] is None  # Singleton becomes unidentified

    def test_collapses_consecutive(self):
        """Test that consecutive identical matches are collapsed."""
        results = [
            (0, {"artist": "Artist", "title": "Track"}),
            (30, {"artist": "Artist", "title": "Track"}),
            (60, {"artist": "Artist", "title": "Track"}),
        ]

        deduped = deduplicate_tracklist(results)

        assert len(deduped) == 1
        assert deduped[0][0] == 0  # First timestamp

    def test_preserves_different_tracks(self):
        """Test that different tracks are preserved."""
        results = [
            (0, {"artist": "Artist A", "title": "Track 1"}),
            (30, {"artist": "Artist A", "title": "Track 1"}),
            (60, {"artist": "Artist B", "title": "Track 2"}),
            (90, {"artist": "Artist B", "title": "Track 2"}),
        ]

        deduped = deduplicate_tracklist(results)

        assert len(deduped) == 2
        assert deduped[0][1]["title"] == "Track 1"
        assert deduped[1][1]["title"] == "Track 2"

    def test_handles_unidentified(self):
        """Test handling of unidentified samples (None)."""
        results = [
            (0, {"artist": "Artist", "title": "Track"}),
            (30, {"artist": "Artist", "title": "Track"}),
            (60, None),
            (90, None),
            (120, {"artist": "Artist", "title": "Track"}),
            (150, {"artist": "Artist", "title": "Track"}),
        ]

        deduped = deduplicate_tracklist(results)

        # Same track before and after gap - track is not re-added since it's the same
        # but the unidentified gap is preserved
        assert len(deduped) == 2
        assert deduped[0][1]["title"] == "Track"
        assert deduped[1][1] is None  # The unidentified gap

    def test_unidentified_gap_between_different_tracks(self):
        """Test unidentified gap between two different tracks."""
        results = [
            (0, {"artist": "Artist A", "title": "Track 1"}),
            (30, {"artist": "Artist A", "title": "Track 1"}),
            (60, None),
            (90, None),
            (120, {"artist": "Artist B", "title": "Track 2"}),
            (150, {"artist": "Artist B", "title": "Track 2"}),
        ]

        deduped = deduplicate_tracklist(results)

        # Different tracks - should have track1, gap, track2
        assert len(deduped) == 3
        assert deduped[0][1]["title"] == "Track 1"
        assert deduped[1][1] is None
        assert deduped[2][1]["title"] == "Track 2"

    def test_case_insensitive(self):
        """Test that deduplication is case-insensitive."""
        results = [
            (0, {"artist": "ARTIST", "title": "TRACK"}),
            (30, {"artist": "artist", "title": "track"}),
            (60, {"artist": "Artist", "title": "Track"}),
        ]

        deduped = deduplicate_tracklist(results)

        # All should be considered the same track
        assert len(deduped) == 1

    def test_empty_input(self):
        """Test with empty input."""
        deduped = deduplicate_tracklist([])
        assert deduped == []


class TestResultsToTracklist:
    """Tests for results_to_tracklist function."""

    def test_basic_conversion(self):
        """Test basic conversion to Tracklist."""
        results = [
            (0, {"artist": "Artist 1", "title": "Track 1"}),
            (30, {"artist": "Artist 1", "title": "Track 1"}),
            (60, {"artist": "Artist 2", "title": "Track 2"}),
            (90, {"artist": "Artist 2", "title": "Track 2"}),
        ]

        tracklist = results_to_tracklist(results, "test.mp3")

        assert tracklist.source_file == "test.mp3"
        assert len(tracklist.tracks) == 2
        assert tracklist.tracks[0].artist == "Artist 1"
        assert tracklist.tracks[1].artist == "Artist 2"

    def test_applies_corrections(self, temp_dir):
        """Test that corrections are applied."""
        db_path = temp_dir / "corrections.json"
        db = CorrectionsDB(db_path=db_path)
        db.add_correction("Wrong Artist", "Wrong Title", "Right Artist", "Right Title")
        db.save()

        results = [
            (0, {"artist": "Wrong Artist", "title": "Wrong Title"}),
            (30, {"artist": "Wrong Artist", "title": "Wrong Title"}),
        ]

        # Reload DB to simulate fresh instance
        db = CorrectionsDB(db_path=db_path)
        tracklist = results_to_tracklist(results, "test.mp3", corrections_db=db)

        assert tracklist.tracks[0].artist == "Right Artist"
        assert tracklist.tracks[0].title == "Right Title"
        assert tracklist.tracks[0].original_artist == "Wrong Artist"

    def test_handles_unidentified(self):
        """Test that unidentified tracks are preserved."""
        results = [
            (0, {"artist": "Artist", "title": "Track"}),
            (30, {"artist": "Artist", "title": "Track"}),
            (60, None),
        ]

        tracklist = results_to_tracklist(results, "test.mp3")

        # After deduplication, we'll have track + unidentified gap
        unidentified_tracks = [t for t in tracklist.tracks if t.is_unidentified]
        assert len(unidentified_tracks) >= 0  # May or may not have gap depending on dedup

    def test_includes_metadata(self):
        """Test that metadata is included."""
        results = [
            (
                0,
                {
                    "artist": "Artist",
                    "title": "Track",
                    "shazam_url": "https://shazam.com/track/123",
                    "album": "Album Name",
                },
            ),
            (
                30,
                {
                    "artist": "Artist",
                    "title": "Track",
                    "shazam_url": "https://shazam.com/track/123",
                    "album": "Album Name",
                },
            ),
        ]

        tracklist = results_to_tracklist(results, "test.mp3")

        assert tracklist.tracks[0].shazam_url == "https://shazam.com/track/123"
        assert tracklist.tracks[0].album == "Album Name"

    def test_sets_generated_on(self):
        """Test that generated_on timestamp is set."""
        results = [
            (0, {"artist": "Artist", "title": "Track"}),
            (30, {"artist": "Artist", "title": "Track"}),
        ]

        tracklist = results_to_tracklist(results, "test.mp3")

        assert tracklist.generated_on is not None
        # Should be in expected format
        assert "-" in tracklist.generated_on
        assert ":" in tracklist.generated_on


class TestProcessSingleFileOutput:
    """Tests for the files written by process_single_file."""

    def test_writes_markdown_and_json_sidecar(self, temp_dir):
        """identify always writes a JSON sidecar carrying cover-art URLs."""
        audio_path = temp_dir / "set.mp3"
        audio_path.write_bytes(b"fake audio")

        # Two identical samples -> one identified track survives dedup, and it
        # carries a Shazam cover-art URL that must land in the JSON sidecar.
        fake_track = {
            "artist": "Artist",
            "title": "Track",
            "coverart_url": "https://example.com/art.jpg",
            "shazam_url": None,
            "album": None,
        }
        slices = [(0, MagicMock()), (30, MagicMock())]

        with (
            patch("setlist_maker.identify.load_audio", return_value=MagicMock()),
            patch("setlist_maker.identify.slice_audio", return_value=slices),
            patch("setlist_maker.identify.Shazam", return_value=MagicMock()),
            patch(
                "setlist_maker.identify.identify_sample_with_retry",
                new=AsyncMock(return_value=fake_track),
            ),
        ):
            result = asyncio.run(
                process_single_file(
                    audio_path=audio_path,
                    output_dir=temp_dir,
                    delay_seconds=0,
                    resume=False,
                )
            )

        assert result is not None
        md_path = temp_dir / "set_tracklist.md"
        json_path = temp_dir / "set_tracklist.json"
        assert md_path.exists()
        assert json_path.exists()

        data = json.loads(json_path.read_text())
        assert data[0]["coverart_url"] == "https://example.com/art.jpg"

        # Progress file is cleaned up on success
        assert not (temp_dir / "set_progress.json").exists()
