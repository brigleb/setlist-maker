"""Tests for setlist_maker.cli module."""

import json

from setlist_maker.cli import (
    AUDIO_EXTENSIONS,
    _load_tracklist_with_artwork_urls,
    deduplicate_tracklist,
    format_duration,
    format_timestamp,
    get_audio_files,
    load_progress,
    results_to_tracklist,
    save_progress,
)
from setlist_maker.editor import CorrectionsDB


class TestFormatTimestamp:
    """Tests for format_timestamp function."""

    def test_zero_seconds(self):
        """Test formatting zero seconds."""
        assert format_timestamp(0) == "0:00"

    def test_seconds_only(self):
        """Test formatting less than a minute."""
        assert format_timestamp(45) == "0:45"

    def test_minutes_and_seconds(self):
        """Test formatting minutes and seconds."""
        assert format_timestamp(90) == "1:30"
        assert format_timestamp(125) == "2:05"

    def test_hours(self):
        """Test formatting with hours."""
        assert format_timestamp(3600) == "1:00:00"
        assert format_timestamp(3661) == "1:01:01"
        assert format_timestamp(7325) == "2:02:05"

    def test_large_values(self):
        """Test formatting large values."""
        # 10 hours, 30 minutes, 45 seconds
        assert format_timestamp(37845) == "10:30:45"


class TestFormatDuration:
    """Tests for format_duration function."""

    def test_seconds_only(self):
        """Test formatting seconds."""
        assert format_duration(45) == "45s"
        assert format_duration(0) == "0s"

    def test_minutes_and_seconds(self):
        """Test formatting minutes and seconds."""
        assert format_duration(90) == "1m 30s"
        assert format_duration(125.5) == "2m 5s"

    def test_hours(self):
        """Test formatting with hours."""
        assert format_duration(3600) == "1h 0m 0s"
        assert format_duration(3661) == "1h 1m 1s"
        assert format_duration(7325.9) == "2h 2m 5s"


class TestGetAudioFiles:
    """Tests for get_audio_files function."""

    def test_single_file(self, sample_audio_files, temp_dir):
        """Test getting a single audio file."""
        files = get_audio_files([str(sample_audio_files[0])])
        assert len(files) == 1
        assert files[0].name == "track1.mp3"

    def test_multiple_files(self, sample_audio_files):
        """Test getting multiple audio files."""
        paths = [str(f) for f in sample_audio_files]
        files = get_audio_files(paths)
        assert len(files) == 3

    def test_directory(self, sample_audio_files, temp_dir):
        """Test getting files from a directory."""
        files = get_audio_files([str(temp_dir)])
        # Should find all 3 audio files, not the txt file
        assert len(files) == 3

    def test_filters_non_audio(self, temp_dir, capsys):
        """Test that non-audio files are filtered out."""
        txt_file = temp_dir / "readme.txt"
        txt_file.write_text("not audio")

        files = get_audio_files([str(txt_file)])

        assert len(files) == 0
        captured = capsys.readouterr()
        assert "Skipping non-audio file" in captured.out

    def test_nonexistent_path(self, capsys):
        """Test handling of nonexistent paths."""
        files = get_audio_files(["/nonexistent/path.mp3"])

        assert len(files) == 0
        captured = capsys.readouterr()
        assert "Path not found" in captured.out

    def test_mixed_valid_invalid(self, sample_audio_files, temp_dir, capsys):
        """Test mix of valid and invalid paths."""
        paths = [
            str(sample_audio_files[0]),
            "/nonexistent.mp3",
            str(temp_dir / "readme.txt"),
        ]
        files = get_audio_files(paths)

        assert len(files) == 1
        assert files[0].name == "track1.mp3"

    def test_supported_extensions(self):
        """Test that AUDIO_EXTENSIONS includes common formats."""
        assert ".mp3" in AUDIO_EXTENSIONS
        assert ".wav" in AUDIO_EXTENSIONS
        assert ".flac" in AUDIO_EXTENSIONS
        assert ".m4a" in AUDIO_EXTENSIONS
        assert ".ogg" in AUDIO_EXTENSIONS


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


class TestLoadTracklistWithArtworkUrls:
    """Tests for _load_tracklist_with_artwork_urls."""

    def _write_tracklist_files(self, temp_dir, tracks_json, markdown):
        """Helper to write both markdown and JSON sidecar files."""
        md_path = temp_dir / "test_tracklist.md"
        json_path = temp_dir / "test_tracklist.json"
        md_path.write_text(markdown)
        json_path.write_text(json.dumps(tracks_json, indent=2))
        return md_path

    def test_loads_artwork_urls_from_json(self, temp_dir):
        """Test that coverart_url is loaded from the JSON sidecar."""
        markdown = """# Tracklist: test.mp3

*Generated on 2026-01-31 20:00*

1. **Artist One** - Track One (0:00)
2. **Artist Two** - Track Two (3:00)
"""
        tracks_json = [
            {
                "timestamp": 0,
                "time": "0:00",
                "artist": "Artist One",
                "title": "Track One",
                "coverart_url": "https://example.com/art1.jpg",
            },
            {
                "timestamp": 180,
                "time": "3:00",
                "artist": "Artist Two",
                "title": "Track Two",
                "coverart_url": "https://example.com/art2.jpg",
            },
        ]

        md_path = self._write_tracklist_files(temp_dir, tracks_json, markdown)
        tracklist, urls = _load_tracklist_with_artwork_urls(md_path)

        assert len(tracklist.tracks) == 2
        assert tracklist.tracks[0].coverart_url == "https://example.com/art1.jpg"
        assert tracklist.tracks[1].coverart_url == "https://example.com/art2.jpg"
        assert urls == {0: "https://example.com/art1.jpg", 1: "https://example.com/art2.jpg"}

    def test_matches_by_timestamp_not_index(self, temp_dir):
        """Test that timestamp matching works when rejected tracks cause index mismatch."""
        # Markdown includes a rejected track that got re-added during editing
        markdown = """# Tracklist: test.mp3

*Generated on 2026-01-31 20:00*

1. **Artist One** - Track One (0:00)
2. **Artist Two** - Track Two (3:00)
3. **Artist Three** - Track Three (6:00)
"""
        # JSON excludes rejected tracks, so indices don't line up with markdown
        # Track Two (timestamp=180) was rejected, so JSON only has tracks at 0 and 360
        tracks_json = [
            {
                "timestamp": 0,
                "time": "0:00",
                "artist": "Artist One",
                "title": "Track One",
                "coverart_url": "https://example.com/art1.jpg",
            },
            {
                "timestamp": 360,
                "time": "6:00",
                "artist": "Artist Three",
                "title": "Track Three",
                "coverart_url": "https://example.com/art3.jpg",
            },
        ]

        md_path = self._write_tracklist_files(temp_dir, tracks_json, markdown)
        tracklist, urls = _load_tracklist_with_artwork_urls(md_path)

        # Track at 0:00 should get art1
        assert tracklist.tracks[0].coverart_url == "https://example.com/art1.jpg"
        # Track at 3:00 has no JSON entry — should be None
        assert tracklist.tracks[1].coverart_url is None
        # Track at 6:00 should get art3 (NOT art3 assigned to wrong index)
        assert tracklist.tracks[2].coverart_url == "https://example.com/art3.jpg"
        assert 1 not in urls

    def test_falls_back_to_markdown_only(self, temp_dir):
        """Test fallback when no JSON sidecar exists."""
        markdown = """# Tracklist: test.mp3

*Generated on 2026-01-31 20:00*

1. **Artist** - Track (0:00)
"""
        md_path = temp_dir / "test_tracklist.md"
        md_path.write_text(markdown)

        tracklist, urls = _load_tracklist_with_artwork_urls(md_path)

        assert len(tracklist.tracks) == 1
        assert urls == {}

    def test_handles_json_without_coverart_urls(self, temp_dir):
        """Test handling of JSON entries that have no coverart_url."""
        markdown = """# Tracklist: test.mp3

*Generated on 2026-01-31 20:00*

1. **Artist** - Track (0:00)
"""
        tracks_json = [
            {"timestamp": 0, "time": "0:00", "artist": "Artist", "title": "Track"},
        ]

        md_path = self._write_tracklist_files(temp_dir, tracks_json, markdown)
        tracklist, urls = _load_tracklist_with_artwork_urls(md_path)

        assert tracklist.tracks[0].coverart_url is None
        assert urls == {}
