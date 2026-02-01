"""Shared pytest fixtures for setlist-maker tests."""

import tempfile
from pathlib import Path

import pytest

from setlist_maker.editor import CorrectionsDB, Track, Tracklist


@pytest.fixture
def sample_track():
    """A basic Track instance."""
    return Track(
        timestamp=90,
        artist="Daft Punk",
        title="Around the World",
    )


@pytest.fixture
def sample_tracklist():
    """A Tracklist with several tracks."""
    return Tracklist(
        source_file="test_mix.mp3",
        generated_on="2026-01-31 20:00",
        tracks=[
            Track(timestamp=0, artist="Daft Punk", title="Around the World"),
            Track(timestamp=180, artist="The Chemical Brothers", title="Block Rockin' Beats"),
            Track(timestamp=360, artist="", title=""),  # Unidentified
            Track(timestamp=540, artist="Fatboy Slim", title="Praise You"),
        ],
    )


@pytest.fixture
def temp_dir():
    """Provide a temporary directory that's cleaned up after the test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_corrections_db(temp_dir):
    """A CorrectionsDB using a temporary file."""
    db_path = temp_dir / "corrections.json"
    return CorrectionsDB(db_path=db_path)


@pytest.fixture
def sample_markdown():
    """Sample markdown tracklist content."""
    return """# Tracklist: test_mix.mp3

*Generated on 2026-01-31 20:00*

1. **Daft Punk** - Around the World (0:00)
2. **The Chemical Brothers** - Block Rockin' Beats (3:00)
3. *Unidentified* (6:00)
4. **Fatboy Slim** - Praise You (9:00)
"""


@pytest.fixture
def sample_audio_files(temp_dir):
    """Create dummy audio files for testing file discovery."""
    files = []
    for name in ["track1.mp3", "track2.wav", "track3.flac"]:
        path = temp_dir / name
        path.write_bytes(b"dummy audio content")
        files.append(path)

    # Also create a non-audio file
    (temp_dir / "readme.txt").write_text("not an audio file")

    return files
