"""Tests for setlist_maker.editor module."""

from setlist_maker.editor import (
    CorrectionsDB,
    Track,
    Tracklist,
    parse_markdown_tracklist,
)


class TestTrack:
    """Tests for the Track dataclass."""

    def test_time_str_minutes_seconds(self):
        """Test timestamp formatting for times under an hour."""
        track = Track(timestamp=90, artist="Artist", title="Title")
        assert track.time_str == "1:30"

    def test_time_str_hours(self):
        """Test timestamp formatting for times over an hour."""
        track = Track(timestamp=3661, artist="Artist", title="Title")
        assert track.time_str == "1:01:01"

    def test_time_str_zero(self):
        """Test timestamp formatting for zero."""
        track = Track(timestamp=0, artist="Artist", title="Title")
        assert track.time_str == "0:00"

    def test_is_unidentified_false(self, sample_track):
        """Test is_unidentified returns False for identified tracks."""
        assert not sample_track.is_unidentified

    def test_is_unidentified_true(self):
        """Test is_unidentified returns True for empty artist/title."""
        track = Track(timestamp=0, artist="", title="")
        assert track.is_unidentified

    def test_is_unidentified_partial(self):
        """Test is_unidentified with partial info."""
        # Only title = not unidentified
        track = Track(timestamp=0, artist="", title="Some Title")
        assert not track.is_unidentified

        # Only artist = not unidentified
        track = Track(timestamp=0, artist="Some Artist", title="")
        assert not track.is_unidentified

    def test_was_corrected_false_no_originals(self, sample_track):
        """Test was_corrected returns False when no original values stored."""
        assert not sample_track.was_corrected

    def test_was_corrected_false_same_values(self):
        """Test was_corrected returns False when values match originals."""
        track = Track(
            timestamp=0,
            artist="Artist",
            title="Title",
            original_artist="Artist",
            original_title="Title",
        )
        assert not track.was_corrected

    def test_was_corrected_true(self):
        """Test was_corrected returns True when values differ from originals."""
        track = Track(
            timestamp=0,
            artist="Corrected Artist",
            title="Corrected Title",
            original_artist="Original Artist",
            original_title="Original Title",
        )
        assert track.was_corrected

    def test_was_corrected_partial(self):
        """Test was_corrected with only one field changed."""
        track = Track(
            timestamp=0,
            artist="Same Artist",
            title="New Title",
            original_artist="Same Artist",
            original_title="Old Title",
        )
        assert track.was_corrected


class TestTracklist:
    """Tests for the Tracklist dataclass."""

    def test_to_markdown(self, sample_tracklist):
        """Test markdown generation."""
        md = sample_tracklist.to_markdown()

        assert "# Tracklist: test_mix.mp3" in md
        assert "*Generated on 2026-01-31 20:00*" in md
        assert "**Daft Punk** - Around the World (0:00)" in md
        assert "**The Chemical Brothers** - Block Rockin' Beats (3:00)" in md
        assert "*Unidentified* (6:00)" in md
        assert "**Fatboy Slim** - Praise You (9:00)" in md

    def test_to_markdown_excludes_rejected(self, sample_tracklist):
        """Test that rejected tracks are excluded from markdown."""
        sample_tracklist.tracks[1].rejected = True
        md = sample_tracklist.to_markdown()

        assert "Block Rockin' Beats" not in md
        assert "Daft Punk" in md
        assert "Fatboy Slim" in md

    def test_to_markdown_renumbers_after_rejection(self, sample_tracklist):
        """Test that track numbers are recalculated after rejections."""
        sample_tracklist.tracks[0].rejected = True
        md = sample_tracklist.to_markdown()

        # Should start with 1, not skip
        assert "1. **The Chemical Brothers**" in md
        # Should be renumbered
        assert "2. *Unidentified*" in md

    def test_to_json(self, sample_tracklist):
        """Test JSON export."""
        data = sample_tracklist.to_json()

        assert len(data) == 4
        assert data[0]["artist"] == "Daft Punk"
        assert data[0]["title"] == "Around the World"
        assert data[0]["timestamp"] == 0
        assert data[0]["time"] == "0:00"
        assert data[0]["rejected"] is False

    def test_to_json_excludes_rejected(self, sample_tracklist):
        """Test that rejected tracks are excluded from JSON."""
        sample_tracklist.tracks[0].rejected = True
        data = sample_tracklist.to_json()

        assert len(data) == 3
        assert all(t["artist"] != "Daft Punk" for t in data)

    def test_empty_tracklist(self):
        """Test handling of empty tracklist."""
        tracklist = Tracklist(source_file="empty.mp3", tracks=[])
        md = tracklist.to_markdown()

        assert "# Tracklist: empty.mp3" in md
        assert "*Generated on" in md

        data = tracklist.to_json()
        assert data == []


class TestParseMarkdownTracklist:
    """Tests for parse_markdown_tracklist function."""

    def test_parse_basic(self, sample_markdown):
        """Test parsing a basic markdown tracklist."""
        tracklist = parse_markdown_tracklist(sample_markdown)

        assert tracklist.source_file == "test_mix.mp3"
        assert tracklist.generated_on == "2026-01-31 20:00"
        assert len(tracklist.tracks) == 4

    def test_parse_tracks(self, sample_markdown):
        """Test that tracks are parsed correctly."""
        tracklist = parse_markdown_tracklist(sample_markdown)

        assert tracklist.tracks[0].artist == "Daft Punk"
        assert tracklist.tracks[0].title == "Around the World"
        assert tracklist.tracks[0].timestamp == 0

        assert tracklist.tracks[1].artist == "The Chemical Brothers"
        assert tracklist.tracks[1].timestamp == 180  # 3:00

    def test_parse_unidentified(self, sample_markdown):
        """Test parsing unidentified tracks."""
        tracklist = parse_markdown_tracklist(sample_markdown)

        unidentified = tracklist.tracks[2]
        assert unidentified.artist == ""
        assert unidentified.title == ""
        assert unidentified.timestamp == 360  # 6:00
        assert unidentified.is_unidentified

    def test_parse_hour_timestamp(self):
        """Test parsing timestamps with hours."""
        md = """# Tracklist: long_mix.mp3

*Generated on 2026-01-31*

1. **Artist** - Song (1:30:45)
"""
        tracklist = parse_markdown_tracklist(md)
        assert tracklist.tracks[0].timestamp == 5445  # 1*3600 + 30*60 + 45

    def test_parse_empty_content(self):
        """Test parsing empty content."""
        tracklist = parse_markdown_tracklist("")
        assert tracklist.source_file == ""
        assert len(tracklist.tracks) == 0

    def test_roundtrip(self, sample_tracklist):
        """Test that to_markdown -> parse_markdown_tracklist preserves data."""
        md = sample_tracklist.to_markdown()
        parsed = parse_markdown_tracklist(md)

        assert parsed.source_file == sample_tracklist.source_file
        assert len(parsed.tracks) == len([t for t in sample_tracklist.tracks if not t.rejected])

        for orig, parsed_track in zip(
            [t for t in sample_tracklist.tracks if not t.rejected], parsed.tracks
        ):
            assert parsed_track.artist == orig.artist
            assert parsed_track.title == orig.title
            assert parsed_track.timestamp == orig.timestamp


class TestCorrectionsDB:
    """Tests for the CorrectionsDB class."""

    def test_add_and_get_correction(self, temp_corrections_db):
        """Test adding and retrieving a correction."""
        temp_corrections_db.add_correction(
            original_artist="Orig Artist",
            original_title="Orig Title",
            corrected_artist="Fixed Artist",
            corrected_title="Fixed Title",
        )

        result = temp_corrections_db.get_correction("Orig Artist", "Orig Title")
        assert result == ("Fixed Artist", "Fixed Title")

    def test_get_correction_not_found(self, temp_corrections_db):
        """Test get_correction returns None for unknown tracks."""
        result = temp_corrections_db.get_correction("Unknown", "Track")
        assert result is None

    def test_case_insensitive_lookup(self, temp_corrections_db):
        """Test that lookups are case-insensitive."""
        temp_corrections_db.add_correction(
            original_artist="Artist Name",
            original_title="Track Title",
            corrected_artist="Fixed",
            corrected_title="Fixed",
        )

        # Should find with different case
        result = temp_corrections_db.get_correction("ARTIST NAME", "TRACK TITLE")
        assert result is not None
        assert result == ("Fixed", "Fixed")

    def test_save_and_load(self, temp_dir):
        """Test persistence of corrections."""
        db_path = temp_dir / "corrections.json"

        # Create and save
        db1 = CorrectionsDB(db_path=db_path)
        db1.add_correction("Orig", "Title", "Fixed", "Title")
        db1.save()

        # Load in new instance
        db2 = CorrectionsDB(db_path=db_path)
        result = db2.get_correction("Orig", "Title")
        assert result == ("Fixed", "Title")

    def test_apply_corrections(self, temp_corrections_db, sample_tracklist):
        """Test applying corrections to a tracklist."""
        temp_corrections_db.add_correction(
            original_artist="Daft Punk",
            original_title="Around the World",
            corrected_artist="Daft Punk",
            corrected_title="Around the World (Album Version)",
        )

        count = temp_corrections_db.apply_corrections(sample_tracklist)

        assert count == 1
        assert sample_tracklist.tracks[0].title == "Around the World (Album Version)"
        assert sample_tracklist.tracks[0].original_title == "Around the World"

    def test_apply_corrections_skips_unidentified(self, temp_corrections_db, sample_tracklist):
        """Test that unidentified tracks are skipped during correction application."""
        # Add a correction for empty strings (shouldn't match)
        temp_corrections_db.add_correction("", "", "Should", "Not Match")

        temp_corrections_db.apply_corrections(sample_tracklist)

        # The unidentified track should not be modified
        unidentified = sample_tracklist.tracks[2]
        assert unidentified.artist == ""
        assert unidentified.title == ""

    def test_load_corrupted_file(self, temp_dir):
        """Test handling of corrupted corrections file."""
        db_path = temp_dir / "corrupted.json"
        db_path.write_text("not valid json {{{")

        # Should not raise, should initialize empty
        db = CorrectionsDB(db_path=db_path)
        assert db.corrections == {}

    def test_load_nonexistent_file(self, temp_dir):
        """Test loading when file doesn't exist."""
        db_path = temp_dir / "nonexistent.json"

        db = CorrectionsDB(db_path=db_path)
        assert db.corrections == {}
