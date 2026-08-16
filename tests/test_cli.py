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
        cover=None,
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

    def test_reopening_recovers_a_fenced_description(self, temp_dir):
        """The reopen path carries the set description back, whatever it says (#16).

        This is the door every saved tracklist comes back through -- both
        editors and `chapters` -- and the markdown is the description's only
        home, so a description lost here is lost for good. A phantom track read
        out of the prose cost more than a wrong row here: the sidecar is joined
        to the markdown positionally for artwork, so it also shifted every
        track's cover by one and put a bogus chapter in the embedded MP3.
        """
        markdown = """# Tracklist: test.mp3

*Generated on 2026-01-31 20:00*

<!-- summary -->
Peaked on 1. **Artist One** - Track One (0:00), a line shaped like a track.
<!-- /summary -->

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
            {"timestamp": 180, "time": "3:00", "artist": "Artist Two", "title": "Track Two"},
        ]

        md_path = self._write_tracklist_files(temp_dir, tracks_json, markdown)
        tracklist, urls = _load_tracklist_with_artwork_urls(md_path)

        assert tracklist.summary == (
            "Peaked on 1. **Artist One** - Track One (0:00), a line shaped like a track."
        )
        assert [t.timestamp for t in tracklist.tracks] == [0, 180]
        assert urls == {0: "https://example.com/art1.jpg"}

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

    @pytest.mark.parametrize("sidecar", ["null", "42", "true", '"a string"', "[1, 2, 3]"])
    def test_structurally_wrong_sidecar_degrades_to_markdown(self, temp_dir, sidecar):
        """A sidecar that parses as JSON but isn't a list of track dicts must not raise.

        json.load succeeds for these, so JSONDecodeError never fires; iterating
        the value (or testing membership in its scalar elements) raises TypeError
        instead. Such a sidecar has to degrade to markdown-only, exactly as an
        unreadable one does -- otherwise it takes down `chapters`, the reuse
        path, and the .md-edit path.
        """
        md_path = temp_dir / "test_tracklist.md"
        md_path.write_text(
            "# Tracklist: test.mp3\n\n*Generated on 2026-01-01 00:00*\n\n"
            "1. **Artist One** - Track One (0:00)\n"
        )
        (temp_dir / "test_tracklist.json").write_text(sidecar)

        tracklist, urls = _load_tracklist_with_artwork_urls(md_path)

        assert [t.title for t in tracklist.tracks] == ["Track One"]
        assert urls == {}
        assert tracklist.tracks[0].coverart_url is None

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


def test_md_edit_path_previews_the_same_key_chapters_embeds(monkeypatch, tmp_path):
    """Editing an existing .md must carry the sidecar's coverart_url into the editor.

    The preview is only authoritative if it is keyed identically to what
    `chapters` embeds. `parse_markdown_tracklist()` alone cannot supply
    coverart_url (markdown has no URL field), so loading a .md straight through
    it made the editor preview chapter_image(artist, title, None) while the
    chapters path -- which reads the JSON sidecar -- embedded
    chapter_image(artist, title, "https://..."): different key, different
    fetch strategy, different image.
    """
    from setlist_maker.artwork import CHAPTER_IMAGE_SIZE
    from setlist_maker.artwork_cache import cache_key

    md_path = tmp_path / "set_tracklist.md"
    md_path.write_text(
        "# Tracklist: set.mp3\n\n"
        "*Generated on 2026-01-31 20:00*\n\n"
        "1. **Daft Punk** - Around the World (0:00)\n"
        "2. **Fatboy Slim** - Praise You (3:00)\n"
    )
    md_path.with_suffix(".json").write_text(
        json.dumps(
            [
                {
                    "timestamp": 0,
                    "time": "0:00",
                    "artist": "Daft Punk",
                    "title": "Around the World",
                    "coverart_url": "https://example.test/art1.jpg",
                },
                {
                    "timestamp": 180,
                    "time": "3:00",
                    "artist": "Fatboy Slim",
                    "title": "Praise You",
                    "coverart_url": "https://example.test/art2.jpg",
                },
            ]
        )
    )

    args = _identify_args(path=str(md_path), web_edit=True)
    with patch("setlist_maker.cli.run_web_editor") as mock_web:
        cmd_identify(args)

    editor_tracklist = mock_web.call_args.args[0]
    chapters_tracklist, _urls = _load_tracklist_with_artwork_urls(md_path)

    def keys(tracklist):
        return [
            cache_key(t.artist, t.title, t.coverart_url, CHAPTER_IMAGE_SIZE)
            for t in tracklist.tracks
        ]

    assert editor_tracklist.tracks[0].coverart_url == "https://example.test/art1.jpg"
    assert keys(editor_tracklist) == keys(chapters_tracklist)


def test_embed_chapters_reuses_cached_artwork(monkeypatch, tmp_path, sample_tracklist):
    """A composite already generated in the editor is not re-fetched at embed time."""
    from setlist_maker import artwork_cache
    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: None,
    )

    # Warm the cache the way the editor's preview would.
    for t in sample_tracklist.tracks:
        if not t.is_unidentified:
            artwork_cache.chapter_image(t.artist, t.title, t.coverart_url)

    def explode(*a, **k):
        raise AssertionError("embed must reuse the cache, not re-fetch")

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", explode)

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    # three identified tracks in the fixture, each with a cached composite
    assert len(embedded["chapter_images"]) == 3
    # no track had real artwork, so there is no episode cover (as before the cache)
    assert embedded["episode_image"] is None


def _jpeg(color=(10, 120, 90)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (600, 600), color).save(buf, format="JPEG")
    return buf.getvalue()


def test_episode_cover_skips_a_track_with_no_artwork(monkeypatch, tmp_path, sample_tracklist):
    """The opener having no findable art must not yield a gradient episode cover.

    Pins the pre-cache behavior: the episode cover comes from the first track
    with *real* artwork, not merely the first identified one.

    Asserts on the episode cover's actual pixel content, not just non-None:
    `chapter_image()` always returns bytes (a gradient fallback on a miss), so
    a mere not-None check passes even when the episode cover silently degrades
    to the gradient -- as happened when the episode cover was (re)built from a
    second, differently-keyed cache lookup instead of reusing this track's own
    fetched art. The gradient fallback's top-left pixel is ~(30, 30, 40); the
    art color here is chosen to be unmistakably different.
    """
    import io

    from PIL import Image

    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    art_color = (200, 20, 21)

    def art_for_second_track_only(artist, title, coverart_url=None, size=600):
        return _jpeg(art_color) if artist == "The Chemical Brothers" else None

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", art_for_second_track_only)

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    assert embedded["episode_image"] is not None
    cover = Image.open(io.BytesIO(embedded["episode_image"])).convert("RGB")
    pixel = cover.getpixel((0, 0))
    tolerance = 20  # allow for JPEG compression drift
    assert all(abs(pixel[c] - art_color[c]) <= tolerance for c in range(3)), (
        f"episode cover top-left pixel {pixel} does not match the fetched art color "
        f"{art_color} (looks like the gradient fallback instead)"
    )


def test_episode_cover_falls_through_when_source_art_is_gone(
    monkeypatch, tmp_path, sample_tracklist
):
    """A cached composite whose .src vanished must not yield a gradient cover.

    With the .src gone (a disk-full window between the .src and .jpg writes, or
    a user pruning .src files to reclaim space) the re-fetch for that track can
    fail. Feeding the resulting None into create_chapter_image() produced a
    gradient *and* assigned it, permanently blocking every later track with
    real art. The pre-cache behavior was to skip to the next track, and that is
    what must hold: the episode cover is either a later track's real art or
    nothing.
    """
    import io

    from PIL import Image

    from setlist_maker import artwork_cache
    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    opener_color = (200, 20, 21)
    later_color = (20, 190, 30)

    # First pass: the opener's art is findable, so its composite gets cached.
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: _jpeg(opener_color),
    )
    opener = sample_tracklist.tracks[0]
    artwork_cache.chapter_image(opener.artist, opener.title, opener.coverart_url)

    # Its .src is then lost, leaving a cached .jpg with no source art behind it.
    key = artwork_cache.cache_key(opener.artist, opener.title, opener.coverart_url, 600)
    (artwork_cache.cache_dir() / f"{key}.src").unlink()

    # Later run: the opener's art can no longer be fetched; a later track's can.
    def fetch(artist, title, coverart_url=None, size=600):
        if artist == opener.artist:
            return None
        return _jpeg(later_color)

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", fetch)

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    cover = embedded["episode_image"]
    assert cover is not None, "a later track had real art and should have supplied the cover"
    pixel = Image.open(io.BytesIO(cover)).convert("RGB").getpixel((0, 0))
    tolerance = 20  # allow for JPEG compression drift
    assert all(abs(pixel[c] - later_color[c]) <= tolerance for c in range(3)), (
        f"episode cover top-left pixel {pixel} is not the later track's art {later_color} "
        f"(the gradient fallback's is ~(30, 30, 40))"
    )


def test_episode_cover_reuses_cached_source_art(monkeypatch, tmp_path, sample_tracklist):
    """The episode cover's second composite must not re-fetch.

    Backs the README claim that a `--chapters` run right after editing needs
    no network: the editor's preview caches this track's raw source art (not
    just its composite), and the episode cover -- a second composite of the
    same track's art, relabelled for the set -- must reuse that cached source
    rather than calling fetch_artwork() again. `test_embed_chapters_reuses_
    cached_artwork` doesn't cover this: with no track having real artwork
    there, the episode-cover branch is never entered.
    """
    from setlist_maker import artwork_cache
    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    real_art = _jpeg((200, 20, 21))
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: real_art,
    )

    # Warm the cache the way the editor's preview would -- this also caches
    # the raw source art (the .src file), not just the composite.
    for t in sample_tracklist.tracks:
        if not t.is_unidentified:
            artwork_cache.chapter_image(t.artist, t.title, t.coverart_url)

    def explode(*a, **k):
        raise AssertionError("episode cover must reuse cached source art, not re-fetch")

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", explode)

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    assert embedded["episode_image"] is not None


def _capture_embed(monkeypatch):
    """Intercept embed_chapters and return the kwargs dict it was called with."""
    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )
    return embedded


def test_cover_image_replaces_episode_cover_and_leaves_chapter_art_alone(
    monkeypatch, tmp_path, sample_tracklist
):
    """--cover overrides the episode cover; per-track chapter images keep their art."""
    import io

    from PIL import Image

    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    track_art = (200, 20, 21)
    cover_art = (20, 200, 30)
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: _jpeg(track_art),
    )
    embedded = _capture_embed(monkeypatch)

    embed_chapters_for_tracklist(
        sample_tracklist, tmp_path / "set.mp3", fetch_art=True, cover_image=_jpeg(cover_art)
    )

    cover = Image.open(io.BytesIO(embedded["episode_image"])).convert("RGB")
    assert cover.getpixel((0, 0))[1] > cover.getpixel((0, 0))[0], (
        "episode cover is not the supplied image"
    )

    # per-track composites still built from the fetched track art
    assert embedded["chapter_images"], "chapter images were skipped"
    first = Image.open(io.BytesIO(next(iter(embedded["chapter_images"].values())))).convert("RGB")
    assert first.getpixel((0, 0))[0] > first.getpixel((0, 0))[1], "chapter art was overwritten"


def test_cover_image_is_embedded_even_with_no_artwork(monkeypatch, tmp_path, sample_tracklist):
    """--cover --no-artwork: the hand-picked cover survives, per-track art is skipped."""
    from setlist_maker.cli import embed_chapters_for_tracklist

    embedded = _capture_embed(monkeypatch)
    cover = _jpeg((20, 200, 30))

    embed_chapters_for_tracklist(
        sample_tracklist, tmp_path / "set.mp3", fetch_art=False, cover_image=cover
    )

    assert embedded["episode_image"] == cover
    assert embedded["chapter_images"] is None


def test_no_cover_keeps_deriving_from_first_track(monkeypatch, tmp_path, sample_tracklist):
    """Without --cover, the pre-existing first-track-with-real-art rule still holds."""
    import io

    from PIL import Image

    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    track_art = (200, 20, 21)
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: _jpeg(track_art),
    )
    embedded = _capture_embed(monkeypatch)

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    cover = Image.open(io.BytesIO(embedded["episode_image"])).convert("RGB")
    assert cover.getpixel((0, 0))[0] > cover.getpixel((0, 0))[1]


def test_cover_without_chapters_is_rejected(tmp_path, capsys):
    """--cover only means something for chapter embedding; say so instead of ignoring it."""
    import pytest

    from setlist_maker.cli import cmd_identify

    cover = tmp_path / "cover.jpg"
    cover.write_bytes(_jpeg((20, 200, 30)))

    with pytest.raises(SystemExit) as exc:
        cmd_identify(_identify_args(cover=str(cover), chapters=False))
    assert exc.value.code == 1
    assert "--chapters" in capsys.readouterr().out


def test_missing_cover_file_fails_before_any_work(tmp_path, capsys):
    """A bad --cover path must stop the run: chapter writing mutates the MP3 in place."""
    import pytest

    from setlist_maker.cli import _resolve_cover

    with pytest.raises(SystemExit) as exc:
        _resolve_cover(str(tmp_path / "nope.jpg"))
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().out


def test_episode_cover_prefers_the_starred_track(monkeypatch, tmp_path, sample_tracklist):
    """The curated choice beats the "first track with real art" default (#20).

    Pixel-level for the same reason as the tests above: chapter_image() always
    returns bytes, so asserting not-None would pass even if the cover silently
    came from the wrong track or degraded to the gradient.
    """
    import io

    from PIL import Image

    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    opener_color = (200, 20, 21)
    starred_color = (20, 21, 200)

    def art_by_artist(artist, title, coverart_url=None, size=600):
        if artist == "Daft Punk":  # the opener: what the old rule would pick
            return _jpeg(opener_color)
        if artist == "Fatboy Slim":  # the last track, starred below
            return _jpeg(starred_color)
        return None

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", art_by_artist)
    sample_tracklist.tracks[3].is_episode_cover = True

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    cover = Image.open(io.BytesIO(embedded["episode_image"])).convert("RGB")
    pixel = cover.getpixel((0, 0))
    assert all(abs(pixel[c] - starred_color[c]) <= 20 for c in range(3)), (
        f"episode cover top-left pixel {pixel} is not the starred track's art "
        f"{starred_color} -- it looks like the opener's, or the gradient"
    )


def test_episode_cover_falls_back_when_the_starred_track_has_no_art(
    monkeypatch, tmp_path, sample_tracklist
):
    """A star is a preference, not a guarantee. A set with a usable cover
    somewhere should still get one rather than none."""
    import io

    from PIL import Image

    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    art_color = (200, 20, 21)

    def art_for_the_opener_only(artist, title, coverart_url=None, size=600):
        return _jpeg(art_color) if artist == "Daft Punk" else None

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", art_for_the_opener_only)
    sample_tracklist.tracks[3].is_episode_cover = True  # Fatboy Slim: nothing findable

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(sample_tracklist, tmp_path / "set.mp3", fetch_art=True)

    cover = Image.open(io.BytesIO(embedded["episode_image"])).convert("RGB")
    pixel = cover.getpixel((0, 0))
    assert all(abs(pixel[c] - art_color[c]) <= 20 for c in range(3)), (
        f"episode cover top-left pixel {pixel} is not the fallback track's art {art_color}"
    )


def test_explicit_cover_flag_still_outranks_a_starred_track(
    monkeypatch, tmp_path, sample_tracklist
):
    """--cover is an explicit override typed at the moment of the run; the star
    is a saved preference. The flag wins, and costs no artwork lookup."""
    from setlist_maker.cli import embed_chapters_for_tracklist

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "setlist_maker.artwork_cache.fetch_artwork",
        lambda artist, title, coverart_url=None, size=600: _jpeg((20, 21, 200)),
    )
    sample_tracklist.tracks[3].is_episode_cover = True

    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters",
        lambda **kw: embedded.update(kw) or kw["audio_path"],
    )

    embed_chapters_for_tracklist(
        sample_tracklist, tmp_path / "set.mp3", fetch_art=True, cover_image=b"supplied-cover"
    )
    assert embedded["episode_image"] == b"supplied-cover"


def test_sidecar_round_trips_the_curation_flags(tmp_path):
    """A curated choice saved by the editor has to reach a later chapters run."""
    from setlist_maker.cli import _load_tracklist_with_artwork_urls

    md_path = tmp_path / "set_tracklist.md"
    md_path.write_text(
        "# Tracklist: set.mp3\n\n*Generated on 2026-01-31 20:00*\n\n"
        "1. **Daft Punk** - Around the World (0:00)\n"
        "2. **Justice** - Genesis (3:00)\n"
    )
    md_path.with_suffix(".json").write_text(
        json.dumps(
            [
                {"timestamp": 0, "artist": "Daft Punk", "title": "Around the World"},
                {
                    "timestamp": 180,
                    "artist": "Justice",
                    "title": "Genesis",
                    "coverart_url": "https://itunes/cross.jpg",
                    "artwork_pinned": True,
                    "episode_cover": True,
                },
            ]
        )
    )

    tracklist, urls = _load_tracklist_with_artwork_urls(md_path)

    assert [t.is_episode_cover for t in tracklist.tracks] == [False, True]
    assert tracklist.tracks[1].artwork_pinned is True
    assert urls == {1: "https://itunes/cross.jpg"}


def test_sidecar_without_curation_flags_still_loads(tmp_path):
    """Sidecars written before this feature carry neither key."""
    from setlist_maker.cli import _load_tracklist_with_artwork_urls

    md_path = tmp_path / "set_tracklist.md"
    md_path.write_text(
        "# Tracklist: set.mp3\n\n*Generated on 2026-01-31 20:00*\n\n"
        "1. **Daft Punk** - Around the World (0:00)\n"
    )
    md_path.with_suffix(".json").write_text(
        json.dumps([{"timestamp": 0, "coverart_url": "https://cdn.shazam.com/a.jpg"}])
    )

    tracklist, urls = _load_tracklist_with_artwork_urls(md_path)
    assert tracklist.tracks[0].coverart_url == "https://cdn.shazam.com/a.jpg"
    assert tracklist.tracks[0].artwork_pinned is False
    assert tracklist.tracks[0].is_episode_cover is False


def test_sidecar_with_a_non_string_coverart_url_does_not_crash(tmp_path):
    """A hand-edited sidecar holding a number here would reach cache_key's str
    join and resize_cover_art_url's re.sub, both outside the loader's guard."""
    from setlist_maker.cli import _load_tracklist_with_artwork_urls

    md_path = tmp_path / "set_tracklist.md"
    md_path.write_text(
        "# Tracklist: set.mp3\n\n*Generated on 2026-01-31 20:00*\n\n"
        "1. **Daft Punk** - Around the World (0:00)\n"
    )
    md_path.with_suffix(".json").write_text(json.dumps([{"timestamp": 0, "coverart_url": 42}]))

    tracklist, urls = _load_tracklist_with_artwork_urls(md_path)
    assert tracklist.tracks[0].coverart_url is None
    assert urls == {}
