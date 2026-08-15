"""Tests for setlist_maker.chapters module."""

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.mp3 import MP3
from PIL import Image

from setlist_maker.chapters import _remove_existing_chapters, embed_chapters
from setlist_maker.editor import Track


def _make_test_jpeg(size: int = 100) -> bytes:
    """Create a minimal test JPEG image."""
    img = Image.new("RGB", (size, size), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=50)
    return buf.getvalue()


def _make_silent_mp3(path: Path, duration_seconds: float = 10.0) -> Path:
    """
    Create a minimal valid MP3 file with silence.

    Creates a file with valid MP3 frame headers containing silent audio.
    """
    # MPEG1 Layer 3, 128kbps, 44100Hz, stereo
    # Frame header: 0xFFFB9004
    # Frame size = 144 * bitrate / sample_rate + padding
    # = 144 * 128000 / 44100 = 417 bytes (without padding)
    frame_size = 417
    samples_per_frame = 1152
    frames_needed = int(duration_seconds * 44100 / samples_per_frame) + 1

    with open(path, "wb") as f:
        for _ in range(frames_needed):
            # Write frame header
            f.write(b"\xff\xfb\x90\x04")
            # Write silent frame data (zeros)
            f.write(b"\x00" * (frame_size - 4))

    return path


@pytest.fixture
def temp_mp3(temp_dir):
    """Create a temporary MP3 file for testing."""
    mp3_path = temp_dir / "test.mp3"
    return _make_silent_mp3(mp3_path, duration_seconds=300.0)


@pytest.fixture
def sample_tracks():
    """Create sample tracks for chapter embedding."""
    return [
        Track(timestamp=0, artist="Daft Punk", title="Around the World"),
        Track(timestamp=90, artist="The Chemical Brothers", title="Block Rockin' Beats"),
        Track(timestamp=210, artist="Fatboy Slim", title="Praise You"),
    ]


class TestEmbedChapters:
    """Tests for embed_chapters function."""

    def test_embeds_basic_chapters(self, temp_mp3, sample_tracks):
        """Test basic chapter embedding without artwork."""
        embed_chapters(temp_mp3, sample_tracks)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        # Should have CTOC frame
        ctoc_keys = [k for k in tags if k.startswith("CTOC:")]
        assert len(ctoc_keys) == 1

        # Should have 3 CHAP frames
        chap_keys = [k for k in tags if k.startswith("CHAP:")]
        assert len(chap_keys) == 3

    def test_chapter_timing(self, temp_mp3, sample_tracks):
        """Test that chapter start/end times are correct."""
        embed_chapters(temp_mp3, sample_tracks)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        # Find chapters and verify timing
        chaps = sorted(
            [tags[k] for k in tags if k.startswith("CHAP:")],
            key=lambda c: c.start_time,
        )

        assert chaps[0].start_time == 0
        assert chaps[0].end_time == 90_000  # Next track starts at 90s

        assert chaps[1].start_time == 90_000
        assert chaps[1].end_time == 210_000  # Next track starts at 210s

        assert chaps[2].start_time == 210_000
        # Last chapter ends at audio duration

    def test_chapter_titles(self, temp_mp3, sample_tracks):
        """Test that chapter titles contain artist - title."""
        embed_chapters(temp_mp3, sample_tracks)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        chaps = sorted(
            [tags[k] for k in tags if k.startswith("CHAP:")],
            key=lambda c: c.start_time,
        )

        # Check TIT2 sub-frame content
        assert "Daft Punk - Around the World" in str(chaps[0].sub_frames.getall("TIT2")[0].text)
        assert "Chemical Brothers" in str(chaps[1].sub_frames.getall("TIT2")[0].text)

    def test_embeds_chapter_artwork(self, temp_mp3, sample_tracks):
        """Test embedding per-chapter artwork."""
        chapter_images = {
            0: _make_test_jpeg(),
            1: _make_test_jpeg(),
            2: _make_test_jpeg(),
        }

        embed_chapters(temp_mp3, sample_tracks, chapter_images=chapter_images)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        chaps = [tags[k] for k in tags if k.startswith("CHAP:")]
        # Each chapter should have an APIC sub-frame
        for chap in chaps:
            apic_keys = [k for k in chap.sub_frames if k.startswith("APIC:")]
            assert len(apic_keys) >= 1

    def test_embeds_episode_artwork(self, temp_mp3, sample_tracks):
        """Test embedding episode-level artwork."""
        episode_img = _make_test_jpeg(200)

        embed_chapters(temp_mp3, sample_tracks, episode_image=episode_img)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        # Should have a top-level APIC frame
        apic_keys = [k for k in tags if k.startswith("APIC:")]
        assert len(apic_keys) >= 1

    def test_ctoc_flags(self, temp_mp3, sample_tracks):
        """Test that CTOC has correct flags (top-level + ordered)."""
        embed_chapters(temp_mp3, sample_tracks)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        ctoc = [tags[k] for k in tags if k.startswith("CTOC:")][0]
        from mutagen.id3 import CTOCFlags

        assert ctoc.flags & CTOCFlags.TOP_LEVEL
        assert ctoc.flags & CTOCFlags.ORDERED

    def test_ctoc_child_ids_match_chapters(self, temp_mp3, sample_tracks):
        """Test that CTOC child IDs reference the CHAP element IDs."""
        embed_chapters(temp_mp3, sample_tracks)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        ctoc = [tags[k] for k in tags if k.startswith("CTOC:")][0]
        chap_ids = {tags[k].element_id for k in tags if k.startswith("CHAP:")}

        for child_id in ctoc.child_element_ids:
            assert child_id in chap_ids

    def test_replaces_existing_chapters(self, temp_mp3, sample_tracks):
        """Test that embedding twice replaces previous chapters."""
        embed_chapters(temp_mp3, sample_tracks)
        embed_chapters(temp_mp3, sample_tracks)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        # Should only have one set of chapters
        chap_keys = [k for k in tags if k.startswith("CHAP:")]
        assert len(chap_keys) == 3

        ctoc_keys = [k for k in tags if k.startswith("CTOC:")]
        assert len(ctoc_keys) == 1

    def test_handles_unidentified_tracks(self, temp_mp3):
        """Test that unidentified tracks get 'Unknown Track' title."""
        tracks = [
            Track(timestamp=0, artist="Artist", title="Title"),
            Track(timestamp=60, artist="", title=""),
        ]

        embed_chapters(temp_mp3, tracks)

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        chaps = sorted(
            [tags[k] for k in tags if k.startswith("CHAP:")],
            key=lambda c: c.start_time,
        )
        assert "Unknown Track" in str(chaps[1].sub_frames.getall("TIT2")[0].text)

    def test_raises_on_missing_file(self, temp_dir):
        """Test that FileNotFoundError is raised for missing audio."""
        tracks = [Track(timestamp=0, artist="A", title="T")]
        with pytest.raises(FileNotFoundError):
            embed_chapters(temp_dir / "nonexistent.mp3", tracks)

    def test_raises_on_empty_tracks(self, temp_mp3):
        """Test that ValueError is raised for empty track list."""
        with pytest.raises(ValueError, match="No tracks"):
            embed_chapters(temp_mp3, [])

    def test_custom_duration(self, temp_mp3, sample_tracks):
        """Test specifying audio duration manually."""
        embed_chapters(
            temp_mp3,
            sample_tracks,
            audio_duration_ms=600_000,  # 10 minutes
        )

        audio = MP3(str(temp_mp3))
        tags = audio.tags

        # Last chapter should end at 600000ms
        chaps = sorted(
            [tags[k] for k in tags if k.startswith("CHAP:")],
            key=lambda c: c.start_time,
        )
        assert chaps[-1].end_time == 600_000


class TestPlayerCompatibility:
    """
    Regression tests for issue #17: chapters written as ID3v2.4 are invisible
    to real players (Apple Podcasts, ffprobe) once an artwork APIC sub-frame
    exceeds 128 bytes, because v2.4 syncsafe sub-frame sizes are widely
    misparsed as plain integers. The tag must be saved as ID3v2.3.
    """

    def test_tag_is_id3v23_on_disk(self, temp_mp3, sample_tracks):
        """The raw tag header must declare version 2.3."""
        embed_chapters(temp_mp3, sample_tracks, chapter_images={0: _make_test_jpeg()})

        header = temp_mp3.read_bytes()[:5]
        assert header[:3] == b"ID3"
        assert header[3] == 3, f"tag is ID3v2.{header[3]}, players need v2.3"

    @pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
    def test_ffprobe_reads_chapters_with_artwork(self, temp_mp3, sample_tracks):
        """
        Round-trip through an independent parser with artwork embedded.

        The artwork sub-frame must exceed 128 bytes — below that, v2.3 and
        v2.4 size encodings coincide and the bug cannot fire.
        """
        image = _make_test_jpeg(200)
        assert len(image) >= 128

        embed_chapters(
            temp_mp3,
            sample_tracks,
            chapter_images={i: image for i in range(len(sample_tracks))},
            episode_image=image,
        )

        result = subprocess.run(
            [
                "ffprobe",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_chapters",
                str(temp_mp3),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        chapters = json.loads(result.stdout)["chapters"]
        assert len(chapters) == len(sample_tracks)

        # Sorted so this stays purely a #17 readability guard; physical CHAP
        # order is TestChapterFrameOrder's job (#33).
        chapters.sort(key=lambda c: int(c["start"]))
        assert [c["tags"]["title"] for c in chapters] == [
            "Daft Punk - Around the World",
            "The Chemical Brothers - Block Rockin' Beats",
            "Fatboy Slim - Praise You",
        ]
        assert [int(c["start"]) for c in chapters] == [0, 90_000, 210_000]

    def test_reembedding_reclaims_tag_space(self, temp_mp3, sample_tracks):
        """
        Re-embedding must not leave the old, larger tag as dead bytes.

        Guards the aftercare path for already-published episodes, whose tags
        carried tens of MB of unreachable residue from repeated embeds.
        """
        # Payloads must dwarf mutagen's keep-padding threshold (10 KiB + 1%)
        # or the freed space is legitimately retained and nothing shrinks.
        big_image = bytes(range(256)) * 800  # ~200 KB, the real artwork cap
        fat_images = {i: big_image for i in range(len(sample_tracks))}
        embed_chapters(temp_mp3, sample_tracks, chapter_images=fat_images)
        fat_size = temp_mp3.stat().st_size

        embed_chapters(temp_mp3, sample_tracks)
        slim_size = temp_mp3.stat().st_size

        assert slim_size < fat_size - 500_000


class TestChapterFrameOrder:
    """
    Regression tests for issue #33: mutagen sorts frames by serialized size at
    save time, so per-chapter artwork of differing sizes scatters CHAP frames
    through the tag. CTOC still orders them for conforming players, but
    ffprobe/ffmpeg -- and hosts and web players built on them -- enumerate CHAP
    frames in file order and show a shuffled chapter list.
    """

    @staticmethod
    def _shrinking_images(tracks):
        """Artwork that gets *smaller* with each chapter.

        mutagen's size sort then writes the CHAP frames in exact reverse, so an
        unfixed embed fails these tests outright instead of passing by luck.
        """
        images = {i: _make_test_jpeg(300 - 100 * i) for i in range(len(tracks))}
        sizes = [len(images[i]) for i in range(len(tracks))]
        assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes)
        return images

    def test_chap_frames_are_chronological_on_disk(self, temp_mp3, sample_tracks):
        """mutagen reads frames back in physical order, so it doubles as the oracle."""
        embed_chapters(
            temp_mp3, sample_tracks, chapter_images=self._shrinking_images(sample_tracks)
        )

        chaps = MP3(str(temp_mp3)).tags.getall("CHAP")
        assert [c.start_time for c in chaps] == [0, 90_000, 210_000]
        assert [c.element_id for c in chaps] == ["chp000", "chp001", "chp002"]

    def test_reordering_only_permutes_the_tag(self, temp_mp3, sample_tracks):
        """The rewrite must not resize the tag or touch a byte of audio."""
        audio_bytes = temp_mp3.read_bytes()  # the fixture has no tag yet: pure audio
        embed_chapters(
            temp_mp3, sample_tracks, chapter_images=self._shrinking_images(sample_tracks)
        )
        data = temp_mp3.read_bytes()
        assert data[:6] == b"ID3\x03\x00\x00"
        tag_size = 0  # syncsafe: 7 bits per byte
        for byte in data[6:10]:
            tag_size = (tag_size << 7) | (byte & 0x7F)
        assert data[10 + tag_size :] == audio_bytes

    @pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
    def test_ffprobe_lists_chapters_in_order(self, temp_mp3, sample_tracks):
        """The real consumer that reads physical order must now see them chronological."""
        embed_chapters(
            temp_mp3, sample_tracks, chapter_images=self._shrinking_images(sample_tracks)
        )

        result = subprocess.run(
            [
                "ffprobe",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_chapters",
                str(temp_mp3),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        chapters = json.loads(result.stdout)["chapters"]
        # Deliberately NOT sorted here: ffprobe reports frames in file order,
        # and that order is exactly what this guards.
        assert [int(c["start"]) for c in chapters] == [0, 90_000, 210_000]
        assert [c["tags"]["title"] for c in chapters] == [
            "Daft Punk - Around the World",
            "The Chemical Brothers - Block Rockin' Beats",
            "Fatboy Slim - Praise You",
        ]


class TestRemoveExistingChapters:
    """Tests for _remove_existing_chapters."""

    def test_removes_chap_and_ctoc(self, temp_mp3, sample_tracks):
        """Test that existing chapters are fully removed."""
        # First embed some chapters
        embed_chapters(temp_mp3, sample_tracks)

        audio = MP3(str(temp_mp3))
        assert any(k.startswith("CHAP:") for k in audio.tags)
        assert any(k.startswith("CTOC:") for k in audio.tags)

        # Remove them
        _remove_existing_chapters(audio)

        assert not any(k.startswith("CHAP:") for k in audio.tags)
        assert not any(k.startswith("CTOC:") for k in audio.tags)

    def test_handles_no_tags(self, temp_mp3):
        """Test that it handles files with no tags gracefully."""
        audio = MP3(str(temp_mp3))
        # Should not raise
        _remove_existing_chapters(audio)
