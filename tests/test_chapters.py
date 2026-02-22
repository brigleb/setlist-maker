"""Tests for setlist_maker.chapters module."""

import io
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
