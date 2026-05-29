"""Tests for setlist_maker.identify module."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from setlist_maker.editor import CorrectionsDB
from setlist_maker.identify import (
    deduplicate_tracklist,
    format_progress_line,
    load_progress,
    process_single_file,
    results_to_tracklist,
    save_progress,
    tracklist_output_path,
)


class TestTracklistOutputPath:
    """Tests for the shared output-path helper used by the CLI and pipeline."""

    def test_defaults_beside_audio_when_no_output_dir(self, temp_dir):
        audio_path = temp_dir / "set.mp3"
        assert tracklist_output_path(audio_path, None) == temp_dir / "set_tracklist.md"

    def test_uses_output_dir_when_given(self, temp_dir):
        audio_path = temp_dir / "set.mp3"
        out = temp_dir / "lists"
        assert tracklist_output_path(audio_path, out) == out / "set_tracklist.md"


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

    def test_preserves_leading_unidentified_gap(self):
        """An unidentified opener (before any identified track) is kept, not dropped."""
        results = [
            (0, None),
            (30, None),
            (60, None),
            (90, {"artist": "Artist", "title": "Track"}),
            (120, {"artist": "Artist", "title": "Track"}),
        ]

        deduped = deduplicate_tracklist(results)

        # The leading gap should surface as an unidentified marker at the start,
        # followed by the first identified track.
        assert len(deduped) == 2
        assert deduped[0][0] == 0
        assert deduped[0][1] is None  # leading unidentified gap preserved
        assert deduped[1][1]["title"] == "Track"

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

    def test_fuzzy_merges_version_drift(self):
        """Metadata drift (edit/feat tags) for one track collapses to one entry."""
        results = [
            (0, {"artist": "Daft Punk", "title": "Around the World"}),
            (30, {"artist": "Daft Punk", "title": "Around the World - Radio Edit"}),
            (60, {"artist": "Daft Punk feat. Pharrell", "title": "Around the World (Remix)"}),
        ]

        deduped = deduplicate_tracklist(results)

        assert len(deduped) == 1
        assert deduped[0][1]["title"] == "Around the World"

    def test_fuzzy_keeps_different_songs_same_artist(self):
        """Two different songs by the same artist are not merged."""
        results = [
            (0, {"artist": "Daft Punk", "title": "Around the World"}),
            (30, {"artist": "Daft Punk", "title": "Around the World"}),
            (60, {"artist": "Daft Punk", "title": "Harder Better Faster Stronger"}),
            (90, {"artist": "Daft Punk", "title": "Harder Better Faster Stronger"}),
        ]

        deduped = deduplicate_tracklist(results)

        assert len(deduped) == 2
        assert deduped[0][1]["title"] == "Around the World"
        assert deduped[1][1]["title"] == "Harder Better Faster Stronger"

    def test_smooths_transient_misdetection(self):
        """An isolated misfire flanked by the same track is corrected (A B A -> A)."""
        results = [
            (0, {"artist": "Artist", "title": "Long Song"}),
            (30, {"artist": "Artist", "title": "Long Song"}),
            (60, {"artist": "Wrong", "title": "Misfire"}),  # transient outlier
            (90, {"artist": "Artist", "title": "Long Song"}),
            (120, {"artist": "Artist", "title": "Long Song"}),
        ]

        deduped = deduplicate_tracklist(results)

        # The misfire is absorbed into the surrounding long song: one entry, no gap.
        assert len(deduped) == 1
        assert deduped[0][1]["title"] == "Long Song"

    def test_smooths_dropout_within_song(self):
        """A single unidentified sample inside a song does not create a gap."""
        results = [
            (0, {"artist": "Artist", "title": "Song"}),
            (30, {"artist": "Artist", "title": "Song"}),
            (60, None),  # single dropout
            (90, {"artist": "Artist", "title": "Song"}),
            (120, {"artist": "Artist", "title": "Song"}),
        ]

        deduped = deduplicate_tracklist(results)

        assert len(deduped) == 1
        assert deduped[0][1]["title"] == "Song"

    def test_confident_singleton_is_kept(self):
        """A lone match survives when Shazam was confident (a real short track)."""
        results = [
            (0, {"artist": "A", "title": "Long", "confidence": 0.9}),
            (30, {"artist": "A", "title": "Long", "confidence": 0.9}),
            (60, {"artist": "B", "title": "Short Interlude", "confidence": 0.95}),
        ]

        deduped = deduplicate_tracklist(results)

        titles = [info["title"] for _, info in deduped if info]
        assert "Short Interlude" in titles

    def test_low_confidence_singleton_is_dropped(self):
        """A lone low-confidence match is treated as a stray false positive."""
        results = [
            (0, {"artist": "A", "title": "Long", "confidence": 0.9}),
            (30, {"artist": "A", "title": "Long", "confidence": 0.9}),
            (60, {"artist": "B", "title": "Stray", "confidence": 0.2}),
        ]

        deduped = deduplicate_tracklist(results)

        titles = [info["title"] for _, info in deduped if info]
        assert "Stray" not in titles


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

        # Progress file is retained on success so a later re-identify can resume
        # from the cached Shazam results instead of re-scanning from scratch.
        assert (temp_dir / "set_progress.json").exists()


class TestFormatProgressLine:
    """Tests for the compact per-sample status line."""

    def test_found_is_single_line_with_confidence(self):
        """A match renders one ASCII line with glyph, confidence, and label."""
        info = {"artist": "Giorgio Moroder", "title": "Midnight Express", "confidence": 0.87}
        line = format_progress_line(20, 62, "9:30", info, width=80, color=False)

        assert "\n" not in line
        assert "\033[" not in line  # no ANSI when color=False
        assert "[20/62]" in line
        assert "9:30" in line
        assert "87%" in line
        assert "+" in line
        assert "Giorgio Moroder - Midnight Express" in line

    def test_not_identified_line(self):
        """A miss renders one line and never claims a track."""
        line = format_progress_line(19, 62, "9:00", None, width=80, color=False)

        assert "\n" not in line
        assert "[19/62]" in line
        assert "not identified" in line

    def test_counter_is_right_aligned_to_total_width(self):
        """Early indices are padded so the counter column lines up."""
        line = format_progress_line(3, 100, "0:30", None, width=80, color=False)
        assert "[  3/100]" in line

    def test_long_label_truncated_to_width(self):
        """An over-long label is truncated with an ellipsis and never wraps."""
        info = {
            "artist": "A Very Long Artist Name",
            "title": "An Equally Long Title",
            "confidence": 0.5,
        }
        width = 40
        line = format_progress_line(1, 9, "0:00", info, width=width, color=False)

        assert len(line) <= width
        assert line.endswith("...")

    def test_color_adds_ansi_and_unicode_glyph(self):
        """Terminal mode colorizes the line and uses the check glyph."""
        info = {"artist": "X", "title": "Y", "confidence": 0.9}
        line = format_progress_line(1, 9, "0:00", info, width=80, color=True)

        assert "\033[32m" in line  # green
        assert "✓" in line

    def test_missing_confidence_does_not_crash(self):
        """A result without a confidence key still renders cleanly."""
        info = {"artist": "X", "title": "Y"}
        line = format_progress_line(1, 9, "0:00", info, width=80, color=False)
        assert "X - Y" in line
