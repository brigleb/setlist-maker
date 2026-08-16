"""Tests for setlist_maker.editor module."""

import pytest

from setlist_maker.editor import (
    SUMMARY_CLOSE_MARKER,
    SUMMARY_OPEN_MARKER,
    CorrectionsDB,
    Track,
    Tracklist,
    TracklistEditor,
    _escape_summary_line,
    _unescape_summary_line,
    apply_track_edit,
    parse_markdown_tracklist,
)


class TestApplyTrackEdit:
    """Tests for the correction step shared by the TUI and web front ends."""

    def _track(self):
        return Track(
            timestamp=0,
            artist="Wrong Artist",
            title="Wrong Title",
            coverart_url="https://cdn.shazam.com/wrong-album.jpg",
        )

    def test_correction_clears_stale_coverart_url(self):
        """The Shazam URL belongs to the original ID; a correction retires it (#30)."""
        track = self._track()
        assert apply_track_edit(track, "Justice", "Genesis") is True
        assert track.artist == "Justice"
        assert track.title == "Genesis"
        assert track.coverart_url is None

    def test_unchanged_edit_keeps_artwork_and_reports_no_change(self):
        """Re-sending an identical row must not discard artwork that is still right."""
        track = self._track()
        assert apply_track_edit(track, "Wrong Artist", "Wrong Title") is False
        assert track.coverart_url == "https://cdn.shazam.com/wrong-album.jpg"
        assert track.original_artist is None  # nothing to remember

    def test_records_correction_and_remembers_original(self, temp_dir):
        db = CorrectionsDB(db_path=temp_dir / "corrections.json")
        track = self._track()
        apply_track_edit(track, "Justice", "Genesis", db)
        assert track.original_artist == "Wrong Artist"
        assert track.original_title == "Wrong Title"
        assert db.get_correction("Wrong Artist", "Wrong Title") == ("Justice", "Genesis")

    def test_second_correction_keeps_the_first_original(self):
        """original_* must stay pinned to what Shazam said, not the last guess."""
        track = self._track()
        apply_track_edit(track, "Justice", "Genesis")
        apply_track_edit(track, "Justice", "D.A.N.C.E.")
        assert track.original_artist == "Wrong Artist"
        assert track.original_title == "Wrong Title"
        assert track.coverart_url is None


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

    def test_to_markdown_includes_summary(self, sample_tracklist):
        """Summary paragraph is fenced, and rendered before the listing.

        The blank lines inside the fence are deliberate: without them a
        renderer that escapes raw HTML folds the markers into the description's
        own paragraph. Asserted here, on the bytes, because nothing downstream
        would notice if they disappeared.
        """
        sample_tracklist.summary = "A propulsive big-beat set with funk-laced breaks."
        md = sample_tracklist.to_markdown()
        lines = md.split("\n")

        summary_idx = lines.index(sample_tracklist.summary)
        first_track_idx = next(i for i, ln in enumerate(lines) if ln.startswith("1. "))
        # Summary comes before the listing, fenced, separated by a blank line.
        assert summary_idx < first_track_idx
        assert lines[summary_idx - 2 : summary_idx] == ["<!-- summary -->", ""]
        assert lines[summary_idx + 1 : summary_idx + 4] == ["", "<!-- /summary -->", ""]

    def test_to_markdown_omits_summary_when_absent(self, sample_tracklist):
        """No summary means no extra prose lines are emitted."""
        assert sample_tracklist.summary is None
        md = sample_tracklist.to_markdown()
        # The only lines before the first track are the header/date block.
        before = md.split("1. ", 1)[0]
        assert before.count("\n\n") == 2  # after the title and after the date


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

    def test_parse_summary(self):
        """A prose paragraph between the date and listing is parsed as the summary.

        This input is the *legacy*, unfenced shape — files written before #16 —
        not what ``to_markdown()`` produces now. It reads like a duplicate of
        the fenced tests below and isn't: it is the back-compat guard.
        """
        md = """# Tracklist: mix.mp3

*Generated on 2026-01-31 20:00*

A driving techno set with dub-inflected low end and hypnotic, minimal arrangements.

1. **Artist** - Song (0:00)
"""
        tracklist = parse_markdown_tracklist(md)
        assert tracklist.summary == (
            "A driving techno set with dub-inflected low end and hypnotic, minimal arrangements."
        )
        assert len(tracklist.tracks) == 1

    def test_summary_roundtrip(self, sample_tracklist):
        """A summary survives to_markdown -> parse_markdown_tracklist."""
        sample_tracklist.summary = "A propulsive big-beat set with funk-laced breaks."
        parsed = parse_markdown_tracklist(sample_tracklist.to_markdown())
        assert parsed.summary == sample_tracklist.summary

    def test_parse_no_summary(self, sample_markdown):
        """Markdown without a summary parses to summary=None."""
        tracklist = parse_markdown_tracklist(sample_markdown)
        assert tracklist.summary is None


class TestSummaryRoundTrip:
    """A set description survives reopen whatever the user typed into it (#16).

    The description is free text the user types in the web editor, so it can
    contain any shape the tracklist format itself uses. Each case below used to
    be silently destructive: the summary was truncated or lost outright, and a
    description line shaped like a track was re-read as a real one, so reopening
    a set grew a phantom track at whatever timestamp the prose happened to name.
    """

    EXPECTED_TRACKS = [
        ("Daft Punk", "Around the World", 0),
        ("The Chemical Brothers", "Block Rockin' Beats", 180),
        ("", "", 360),
        ("Fatboy Slim", "Praise You", 540),
    ]

    @pytest.mark.parametrize(
        "summary",
        [
            pytest.param("1. **Test** - Song (0:00)", id="issue-repro-track-shaped-line"),
            pytest.param("2. *Unidentified* (6:00)", id="unidentified-track-shape"),
            pytest.param(
                "Opens with 1. **Test** - Song (0:00) and never looks back.",
                id="track-shape-mid-sentence",
            ),
            pytest.param("*Generated on* a rainy Tuesday.", id="collides-with-date-line"),
            pytest.param("# Tracklist: not-a-real-file.mp3", id="collides-with-header"),
            pytest.param("First paragraph.\n\nSecond paragraph.", id="blank-line-inside"),
            pytest.param("A set with\ntwo prose lines.", id="hard-wrapped"),
            pytest.param("Ends on a numbered thought.\n\n1. yes", id="trailing-list"),
        ],
    )
    def test_description_survives_and_invents_no_tracks(self, sample_tracklist, summary):
        sample_tracklist.summary = summary
        parsed = parse_markdown_tracklist(sample_tracklist.to_markdown())

        assert parsed.summary == summary
        assert [(t.artist, t.title, t.timestamp) for t in parsed.tracks] == self.EXPECTED_TRACKS

    @pytest.mark.parametrize(
        "summary",
        [
            pytest.param(SUMMARY_CLOSE_MARKER, id="is-the-closing-marker"),
            pytest.param(SUMMARY_OPEN_MARKER, id="is-the-opening-marker"),
            pytest.param("\\" + SUMMARY_CLOSE_MARKER, id="is-an-escaped-marker"),
            pytest.param("Fenced like\n<!-- /summary -->\nthis.", id="marker-on-an-inner-line"),
        ],
    )
    def test_a_description_cannot_close_its_own_fence(self, sample_tracklist, summary):
        """The one line that could break the fence is escaped on the way out."""
        sample_tracklist.summary = summary
        parsed = parse_markdown_tracklist(sample_tracklist.to_markdown())

        assert parsed.summary == summary
        assert [(t.artist, t.title, t.timestamp) for t in parsed.tracks] == self.EXPECTED_TRACKS

    @pytest.mark.parametrize(
        "line",
        [
            "ordinary prose",
            SUMMARY_OPEN_MARKER,
            SUMMARY_CLOSE_MARKER,
            "\\" + SUMMARY_CLOSE_MARKER,
            "\\\\" + SUMMARY_OPEN_MARKER,
            "  " + SUMMARY_CLOSE_MARKER + "  ",
            "<!-- summary --> trailing",
        ],
    )
    def test_escaping_a_line_is_an_involution(self, line):
        """Stated as the property it is, at any backslash depth.

        A near-miss like ``<!-- summary --> trailing`` must come back with no
        backslash it did not have: escaping more than the fence would put marks
        in the user's prose, which is its own small silent corruption.
        """
        assert _unescape_summary_line(_escape_summary_line(line)) == line

    def test_header_and_date_come_from_the_file_not_the_description(self, sample_tracklist):
        """The fence's line span is excluded from *every* scan, not just tracks.

        Excluding it from the track scan alone still passes the round-trip
        checks above while silently taking the source filename and generation
        date from prose — and rewriting both into the file on the next save.
        """
        sample_tracklist.summary = "# Tracklist: fake.mp3\n*Generated on 1999-01-01 00:00*"
        parsed = parse_markdown_tracklist(sample_tracklist.to_markdown())

        assert parsed.source_file == "test_mix.mp3"
        assert parsed.generated_on == "2026-01-31 20:00"
        assert parsed.summary == sample_tracklist.summary

    def test_empty_fence_parses_as_no_summary(self):
        """A cleared description leaves None, not an empty string."""
        md = f"""# Tracklist: mix.mp3

*Generated on 2026-01-31 20:00*

{SUMMARY_OPEN_MARKER}
{SUMMARY_CLOSE_MARKER}

1. **Artist** - Song (0:00)
"""
        tracklist = parse_markdown_tracklist(md)

        assert tracklist.summary is None
        assert len(tracklist.tracks) == 1

    def test_whitespace_only_description_writes_no_fence(self, sample_tracklist):
        """Nothing to fence means no fence — the file gains nothing."""
        sample_tracklist.summary = "   "
        md = sample_tracklist.to_markdown()

        assert SUMMARY_OPEN_MARKER not in md
        assert parse_markdown_tracklist(md).summary is None

    def test_resaving_is_byte_identical(self, sample_tracklist):
        """Reopening and saving again must not accumulate fences.

        The failure this pins is quiet and cumulative: a parser that reads its
        own markers back as prose re-wraps them on every save, so the file grows
        a nested pair per round trip and the description drifts further from
        what the user typed each time.
        """
        sample_tracklist.summary = "1. **Test** - Song (0:00)"
        once = sample_tracklist.to_markdown()
        twice = parse_markdown_tracklist(once).to_markdown()

        assert twice == once
        assert once.count(SUMMARY_OPEN_MARKER) == 1

    def test_legacy_markdown_upgrades_in_place_on_the_next_save(self):
        """An old file's description is carried into the fence, not dropped."""
        legacy = """# Tracklist: mix.mp3

*Generated on 2026-01-31 20:00*

A driving techno set.

1. **Artist** - Song (0:00)
"""
        upgraded = parse_markdown_tracklist(legacy).to_markdown()

        assert SUMMARY_OPEN_MARKER in upgraded
        assert parse_markdown_tracklist(upgraded).summary == "A driving techno set."

    def test_a_carriage_return_cannot_split_a_line_open(self, sample_tracklist, tmp_path):
        """A lone CR is a line break to the reader but not to the writer.

        So it has to be one before the file is written: a description carrying
        ``\\r<!-- /summary -->`` would otherwise reach disk unescaped, be split
        there by the text-mode read, and close the fence from the inside --
        this bug again, one input class over. Written through a real file
        because it is ``open()``'s newline translation that does the splitting.
        """
        sample_tracklist.summary = "Opening line.\r<!-- /summary -->\r1. **Ghost** - It (9:00)"
        path = tmp_path / "set_tracklist.md"
        path.write_text(sample_tracklist.to_markdown())

        with open(path) as f:
            parsed = parse_markdown_tracklist(f.read())

        assert parsed.summary == "Opening line.\n<!-- /summary -->\n1. **Ghost** - It (9:00)"
        assert [(t.artist, t.title, t.timestamp) for t in parsed.tracks] == self.EXPECTED_TRACKS

    def test_fence_closed_past_the_listing_says_so(self, capsys):
        """Swallowing every track is loud, because the next save would seal it.

        Only a hand edit can put the closing marker below the listing, and the
        parse can't tell which half was meant to be prose -- but saving over it
        would write the mistake back as a well-formed file and take the
        sidecar's artwork with it, so this refuses to be quiet about it.
        """
        md = f"""# Tracklist: mix.mp3

*Generated on 2026-01-31 20:00*

{SUMMARY_OPEN_MARKER}
A techno set.

1. **Daft Punk** - Around the World (0:00)
2. **Kraftwerk** - Autobahn (3:00)

{SUMMARY_CLOSE_MARKER}
"""
        tracklist = parse_markdown_tracklist(md)

        assert tracklist.tracks == []
        assert "runs past the listing" in capsys.readouterr().out

    def test_unclosed_fence_does_not_swallow_the_listing(self, capsys):
        """A hand edit that loses the closing marker degrades, it doesn't destroy.

        Reading to end-of-file would take the whole listing for prose and return
        a tracklist with no tracks, so an unterminated fence falls back to the
        legacy scan instead -- out loud, because a half-fence is a damaged file
        rather than an old one, and this fix is about not losing text quietly.
        """
        md = f"""# Tracklist: mix.mp3

*Generated on 2026-01-31 20:00*

{SUMMARY_OPEN_MARKER}
A driving techno set.

1. **Artist** - Song (0:00)
2. **Other** - Tune (4:00)
"""
        tracklist = parse_markdown_tracklist(md)

        assert tracklist.summary == "A driving techno set."
        assert [t.timestamp for t in tracklist.tracks] == [0, 240]
        assert "no closing marker" in capsys.readouterr().out

    def test_legacy_markdown_without_delimiters_still_parses(self, capsys):
        """Files written before the delimiter existed keep reopening (back-compat).

        Their descriptions are still ambiguous prose -- nothing can recover a
        line the old format made indistinguishable from a track -- but an
        ordinary one must survive untouched until the next save rewrites it.
        """
        md = """# Tracklist: mix.mp3

*Generated on 2026-01-31 20:00*

A driving techno set with dub-inflected low end.

1. **Artist** - Song (0:00)
2. **Other** - Tune (4:00)
"""
        tracklist = parse_markdown_tracklist(md)

        assert tracklist.summary == "A driving techno set with dub-inflected low end."
        assert [t.timestamp for t in tracklist.tracks] == [0, 240]
        # An old file is expected, not damaged: reading one is not worth a warning.
        assert capsys.readouterr().out == ""


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


def test_save_tracklist_writes_markdown_and_json(tmp_path):
    """save_tracklist writes both the .md and the .json sidecar."""
    from setlist_maker.editor import save_tracklist

    tl = Tracklist(
        source_file="set.mp3",
        generated_on="2026-01-01 00:00",
        tracks=[
            Track(timestamp=0, artist="A", title="One"),
            Track(timestamp=60, artist="B", title="Two", rejected=True),
        ],
    )
    out = tmp_path / "set_tracklist.md"

    save_tracklist(tl, out, corrections_db=None)

    assert out.read_text() == tl.to_markdown()
    import json as _json

    written = _json.loads((tmp_path / "set_tracklist.json").read_text())
    # rejected tracks are excluded from JSON output
    assert [t["artist"] for t in written] == ["A"]


def test_resolve_audio_path_prefers_explicit(tmp_path):
    """An explicit, existing audio path wins over sibling discovery."""
    from setlist_maker.editor import resolve_audio_path

    audio = tmp_path / "given.mp3"
    audio.write_bytes(b"x")
    out = tmp_path / "set_tracklist.md"
    assert resolve_audio_path(audio, out) == audio


def test_resolve_audio_path_falls_back_to_sibling(tmp_path):
    """With no explicit path, it discovers a sibling of the markdown file."""
    from setlist_maker.editor import resolve_audio_path

    sibling = tmp_path / "set.mp3"
    sibling.write_bytes(b"x")
    out = tmp_path / "set_tracklist.md"
    assert resolve_audio_path(None, out) == sibling


def test_resolve_audio_path_none_when_missing(tmp_path):
    """Returns None when nothing resolves."""
    from setlist_maker.editor import resolve_audio_path

    assert resolve_audio_path(None, tmp_path / "set_tracklist.md") is None


class TestResolveAudioPath:
    """Tests for TracklistEditor._resolve_audio_path() (no DOM required)."""

    def _editor(self, output_path, audio_path=None):
        tracklist = Tracklist(
            source_file="set.mp3", tracks=[Track(timestamp=0, artist="", title="")]
        )
        return TracklistEditor(tracklist, output_path, corrections_db=None, audio_path=audio_path)

    def test_uses_explicit_audio_path_when_it_exists(self, temp_dir):
        audio = temp_dir / "set.mp3"
        audio.write_bytes(b"x")
        md = temp_dir / "set_tracklist.md"
        editor = self._editor(md, audio_path=audio)
        assert editor._resolve_audio_path() == audio

    def test_falls_back_to_discovery_when_no_explicit_path(self, temp_dir):
        # Sibling audio file matching the markdown stem (minus _tracklist).
        audio = temp_dir / "set.mp3"
        audio.write_bytes(b"x")
        md = temp_dir / "set_tracklist.md"
        editor = self._editor(md, audio_path=None)
        assert editor._resolve_audio_path() == audio

    def test_returns_none_when_unresolvable(self, temp_dir):
        md = temp_dir / "set_tracklist.md"
        editor = self._editor(md, audio_path=None)
        assert editor._resolve_audio_path() is None

    def test_ignores_explicit_path_that_does_not_exist(self, temp_dir):
        # A stale/moved explicit path falls through to discovery (which also
        # fails here), rather than being returned blindly.
        missing = temp_dir / "moved" / "set.mp3"
        md = temp_dir / "set_tracklist.md"
        editor = self._editor(md, audio_path=missing)
        assert editor._resolve_audio_path() is None


class TestPinnedArtwork:
    """A cover the user picked outlives a later text correction (#20)."""

    def _pinned(self):
        return Track(
            timestamp=0,
            artist="Daft Punk",
            title="Around the Wold",
            coverart_url="https://itunes/discovery.jpg",
            artwork_pinned=True,
        )

    def test_correction_keeps_a_pinned_coverart_url(self):
        """#30 clears the URL because it is Shazam's evidence about the *old*
        identification. A picked cover is the opposite -- the user's answer
        about this track -- so a typo fix must not throw it away."""
        track = self._pinned()
        assert apply_track_edit(track, "Daft Punk", "Around the World") is True
        assert track.coverart_url == "https://itunes/discovery.jpg"

    def test_correction_still_clears_an_unpinned_coverart_url(self):
        """The #30 behavior is untouched for art nobody chose."""
        track = self._pinned()
        track.artwork_pinned = False
        assert apply_track_edit(track, "Justice", "Genesis") is True
        assert track.coverart_url is None

    def test_tracks_default_to_unpinned_and_uncovered(self):
        track = Track(timestamp=0, artist="A", title="B")
        assert track.artwork_pinned is False
        assert track.is_episode_cover is False


class TestCurationRoundTrip:
    """The curated choices ride in the JSON sidecar, which stays a bare list."""

    def test_to_json_carries_the_curation_fields(self):
        tracklist = Tracklist(source_file="set.mp3")
        tracklist.tracks = [
            Track(
                timestamp=0,
                artist="Daft Punk",
                title="Around the World",
                coverart_url="https://itunes/discovery.jpg",
                artwork_pinned=True,
                is_episode_cover=True,
            )
        ]
        exported = tracklist.to_json()
        assert isinstance(exported, list), "the sidecar's top level must not become an object"
        assert exported[0]["coverart_url"] == "https://itunes/discovery.jpg"
        assert exported[0]["artwork_pinned"] is True
        assert exported[0]["episode_cover"] is True

    def test_untouched_tracks_export_the_fields_as_false(self):
        tracklist = Tracklist(source_file="set.mp3")
        tracklist.tracks = [Track(timestamp=0, artist="A", title="B")]
        assert tracklist.to_json()[0]["artwork_pinned"] is False
        assert tracklist.to_json()[0]["episode_cover"] is False
