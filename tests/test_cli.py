"""Tests for setlist_maker.cli module."""

import json
from unittest.mock import patch

from setlist_maker.cli import (
    _chain_chapters_after_identify,
    _load_tracklist_with_artwork_urls,
)
from setlist_maker.editor import Track, Tracklist


class TestChainChaptersAfterIdentify:
    """Tests for the identify --chapters chaining guard logic."""

    def _write_tracklist(self, temp_dir, source_file):
        """Write a markdown + JSON sidecar for a one-track tracklist."""
        tracklist = Tracklist(
            source_file=source_file,
            tracks=[Track(timestamp=0, artist="Artist", title="Track")],
            generated_on="2026-01-01 00:00",
        )
        md_path = temp_dir / "set_tracklist.md"
        md_path.write_text(tracklist.to_markdown())
        (temp_dir / "set_tracklist.json").write_text(json.dumps(tracklist.to_json()))
        return tracklist, md_path

    def test_embeds_for_mp3(self, temp_dir):
        """An MP3 input triggers chapter embedding."""
        mp3 = temp_dir / "set.mp3"
        mp3.write_bytes(b"fake")
        tracklist, md_path = self._write_tracklist(temp_dir, "set.mp3")

        with patch("setlist_maker.cli.embed_chapters_for_tracklist") as mock_embed:
            _chain_chapters_after_identify([(tracklist, md_path)], [mp3], fetch_art=False)
            mock_embed.assert_called_once()

    def test_skips_non_mp3(self, temp_dir, capsys):
        """A non-MP3 input is skipped with a clear message, not embedded."""
        wav = temp_dir / "set.wav"
        wav.write_bytes(b"fake")
        tracklist, md_path = self._write_tracklist(temp_dir, "set.wav")

        with patch("setlist_maker.cli.embed_chapters_for_tracklist") as mock_embed:
            _chain_chapters_after_identify([(tracklist, md_path)], [wav], fetch_art=False)
            mock_embed.assert_not_called()

        assert "require an MP3" in capsys.readouterr().out


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
