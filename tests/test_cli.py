"""Tests for setlist_maker.cli module."""

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from setlist_maker.cli import (
    _chain_chapters_after_identify,
    _load_tracklist_with_artwork_urls,
    cmd_identify,
)
from setlist_maker.editor import Track, Tracklist
from setlist_maker.identify import (
    ARTIST_SIMILARITY_THRESHOLD,
    SIMILARITY_THRESHOLD,
    SINGLETON_CONFIDENCE_KEEP,
)


def _identify_args(**overrides):
    """Build an argparse.Namespace with identify defaults, overridable per test."""
    defaults = dict(
        path="set.mp3",
        output_dir=None,
        delay=0,
        edit=False,
        chapters=False,
        no_artwork=False,
        no_resume=False,
        allow_partial=False,
        no_learn=True,  # disabled so these unit tests never touch the real corrections DB
        no_summary=False,
        reidentify=False,
        title_threshold=SIMILARITY_THRESHOLD,
        artist_threshold=ARTIST_SIMILARITY_THRESHOLD,
        singleton_confidence=SINGLETON_CONFIDENCE_KEEP,
        no_smoothing=False,
        web_edit=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _dummy_result():
    """A minimal (Tracklist, output_path) tuple as returned by process_single_file."""
    tracklist = Tracklist(
        source_file="set.mp3",
        tracks=[Track(timestamp=0, artist="Artist", title="Track")],
        generated_on="2026-01-01 00:00",
    )
    return tracklist, Path("set_tracklist.md")


class TestIdentifyTuningFlags:
    """Tests that detection-tuning flags flow into the DedupConfig."""

    def test_flags_build_dedup_config(self):
        """Custom tuning flags are passed through to process_single_file."""
        args = _identify_args(
            title_threshold=0.7,
            artist_threshold=0.95,
            singleton_confidence=0.4,
            no_smoothing=True,
        )

        with (
            patch("setlist_maker.cli.get_audio_file", return_value=Path("set.mp3")),
            patch(
                "setlist_maker.cli.process_single_file",
                new=AsyncMock(return_value=_dummy_result()),
            ) as mock_process,
        ):
            cmd_identify(args)

        config = mock_process.call_args.kwargs["dedup_config"]
        assert config.title_threshold == 0.7
        assert config.artist_threshold == 0.95
        assert config.singleton_confidence_keep == 0.4
        assert config.smoothing is False

    def test_defaults_when_flags_omitted(self):
        """Without flags the config uses the module default thresholds."""
        with (
            patch("setlist_maker.cli.get_audio_file", return_value=Path("set.mp3")),
            patch(
                "setlist_maker.cli.process_single_file",
                new=AsyncMock(return_value=_dummy_result()),
            ) as mock_process,
        ):
            cmd_identify(_identify_args())

        config = mock_process.call_args.kwargs["dedup_config"]
        assert config.title_threshold == SIMILARITY_THRESHOLD
        assert config.artist_threshold == ARTIST_SIMILARITY_THRESHOLD
        assert config.singleton_confidence_keep == SINGLETON_CONFIDENCE_KEEP
        assert config.smoothing is True

    def test_summary_enabled_by_default(self):
        """Without --no-summary, process_single_file is asked to generate one."""
        with (
            patch("setlist_maker.cli.get_audio_file", return_value=Path("set.mp3")),
            patch(
                "setlist_maker.cli.process_single_file",
                new=AsyncMock(return_value=_dummy_result()),
            ) as mock_process,
        ):
            cmd_identify(_identify_args())

        assert mock_process.call_args.kwargs["summary"] is True

    def test_no_summary_flag_disables_summary(self):
        """--no-summary propagates as summary=False to process_single_file."""
        with (
            patch("setlist_maker.cli.get_audio_file", return_value=Path("set.mp3")),
            patch(
                "setlist_maker.cli.process_single_file",
                new=AsyncMock(return_value=_dummy_result()),
            ) as mock_process,
        ):
            cmd_identify(_identify_args(no_summary=True))

        assert mock_process.call_args.kwargs["summary"] is False

    def test_allow_partial_off_by_default(self):
        """Without --allow-partial, the decode-completeness guard stays enabled."""
        with (
            patch("setlist_maker.cli.get_audio_file", return_value=Path("set.mp3")),
            patch(
                "setlist_maker.cli.process_single_file",
                new=AsyncMock(return_value=_dummy_result()),
            ) as mock_process,
        ):
            cmd_identify(_identify_args())

        assert mock_process.call_args.kwargs["allow_partial"] is False

    def test_allow_partial_flag_propagates(self):
        """--allow-partial propagates as allow_partial=True to process_single_file."""
        with (
            patch("setlist_maker.cli.get_audio_file", return_value=Path("set.mp3")),
            patch(
                "setlist_maker.cli.process_single_file",
                new=AsyncMock(return_value=_dummy_result()),
            ) as mock_process,
        ):
            cmd_identify(_identify_args(allow_partial=True))

        assert mock_process.call_args.kwargs["allow_partial"] is True

    def test_out_of_range_threshold_exits(self, capsys):
        """An out-of-range tuning value fails fast before any processing."""
        with (
            patch("setlist_maker.cli.get_audio_file") as mock_file,
            patch("setlist_maker.cli.process_single_file", new=AsyncMock()) as mock_process,
            pytest.raises(SystemExit),
        ):
            cmd_identify(_identify_args(title_threshold=1.5))

        mock_file.assert_not_called()
        mock_process.assert_not_called()
        assert "between 0.0 and 1.0" in capsys.readouterr().out


class TestIdentifyReusesExistingTracklist:
    """Pointing identify at audio reuses an already-generated tracklist."""

    def _seed(self, temp_dir, *, tracks=True):
        """Create an audio file and (optionally) a matching tracklist beside it."""
        audio = temp_dir / "set.mp3"
        audio.write_bytes(b"fake audio")
        md = temp_dir / "set_tracklist.md"
        if tracks:
            tracklist = Tracklist(
                source_file="set.mp3",
                tracks=[Track(timestamp=0, artist="Artist", title="Track")],
                generated_on="2026-01-01 00:00",
            )
            md.write_text(tracklist.to_markdown())
        else:
            # A header-only file with no numbered tracks parses to zero tracks.
            md.write_text("# Tracklist: set.mp3\n\n*Generated on 2026-01-01 00:00*\n")
        return audio, md

    def test_existing_tracklist_skips_identification(self, temp_dir, capsys):
        """A bare re-run loads the saved tracklist instead of re-running Shazam."""
        audio, _md = self._seed(temp_dir)
        args = _identify_args(path=str(audio))

        with (
            patch("setlist_maker.cli.get_audio_file", return_value=audio),
            patch("setlist_maker.cli.process_single_file", new=AsyncMock()) as mock_process,
        ):
            cmd_identify(args)

        mock_process.assert_not_called()
        out = capsys.readouterr().out
        assert "set_tracklist.md" in out
        assert "Artist" in out  # the existing tracklist was printed

    def test_existing_tracklist_opens_editor_with_audio(self, temp_dir):
        """--edit on existing audio opens the editor on the saved tracklist."""
        audio, _md = self._seed(temp_dir)
        args = _identify_args(path=str(audio), edit=True)

        with (
            patch("setlist_maker.cli.get_audio_file", return_value=audio),
            patch("setlist_maker.cli.process_single_file", new=AsyncMock()) as mock_process,
            patch("setlist_maker.cli.run_editor") as mock_editor,
        ):
            cmd_identify(args)

        mock_process.assert_not_called()
        mock_editor.assert_called_once()
        assert mock_editor.call_args.kwargs["audio_path"] == audio

    def test_reidentify_forces_identification(self, temp_dir):
        """--reidentify re-runs the pipeline even when a tracklist exists."""
        audio, _md = self._seed(temp_dir)
        args = _identify_args(path=str(audio), reidentify=True)

        with (
            patch("setlist_maker.cli.get_audio_file", return_value=audio),
            patch(
                "setlist_maker.cli.process_single_file",
                new=AsyncMock(return_value=_dummy_result()),
            ) as mock_process,
        ):
            cmd_identify(args)

        mock_process.assert_called_once()

    def test_empty_tracklist_falls_through_to_identification(self, temp_dir):
        """A tracklist with no parseable tracks is ignored; identification runs."""
        audio, _md = self._seed(temp_dir, tracks=False)
        args = _identify_args(path=str(audio))

        with (
            patch("setlist_maker.cli.get_audio_file", return_value=audio),
            patch(
                "setlist_maker.cli.process_single_file",
                new=AsyncMock(return_value=_dummy_result()),
            ) as mock_process,
        ):
            cmd_identify(args)

        mock_process.assert_called_once()


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
        _tracklist, md_path = self._write_tracklist(temp_dir, "set.mp3")

        with patch("setlist_maker.cli.embed_chapters_for_tracklist") as mock_embed:
            _chain_chapters_after_identify(md_path, mp3, fetch_art=False)
            mock_embed.assert_called_once()

    def test_skips_non_mp3(self, temp_dir, capsys):
        """A non-MP3 input is skipped with a clear message, not embedded."""
        wav = temp_dir / "set.wav"
        wav.write_bytes(b"fake")
        _tracklist, md_path = self._write_tracklist(temp_dir, "set.wav")

        with patch("setlist_maker.cli.embed_chapters_for_tracklist") as mock_embed:
            _chain_chapters_after_identify(md_path, wav, fetch_art=False)
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


def test_web_edit_flag_opens_browser_editor():
    """--web-edit routes to run_web_editor, not the TUI run_editor."""
    args = _identify_args(web_edit=True)
    with (
        patch("setlist_maker.cli.get_audio_file", return_value=Path("set.mp3")),
        patch("setlist_maker.cli.process_single_file", new=AsyncMock(return_value=_dummy_result())),
        patch("setlist_maker.cli.run_web_editor") as mock_web,
        patch("setlist_maker.cli.run_editor") as mock_tui,
    ):
        cmd_identify(args)
    mock_web.assert_called_once()
    mock_tui.assert_not_called()


def test_edit_and_web_edit_together_errors():
    """Passing both --edit and --web-edit exits with an error."""
    args = _identify_args(edit=True, web_edit=True)
    with pytest.raises(SystemExit):
        cmd_identify(args)
