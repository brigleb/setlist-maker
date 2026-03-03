"""Tests for setlist_maker.artwork module."""

import io
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw, ImageFont

from setlist_maker.artwork import (
    CHAPTER_IMAGE_SIZE,
    MAX_IMAGE_BYTES,
    _clean_query,
    _compress_to_jpeg,
    _create_fallback_background,
    _draw_text_fitted,
    create_chapter_image,
    fetch_artwork,
    resize_cover_art_url,
    search_deezer_artwork,
    search_itunes_artwork,
    search_musicbrainz_artwork,
)


def _make_test_image(size: int = 600, color: tuple = (255, 0, 0)) -> bytes:
    """Create a simple test JPEG image."""
    img = Image.new("RGB", (size, size), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class TestResizeCoverArtUrl:
    """Tests for resize_cover_art_url."""

    def test_resizes_standard_url(self):
        url = "https://is1-ssl.mzstatic.com/image/400x400cc.jpg"
        result = resize_cover_art_url(url, 600)
        assert "600x600cc" in result

    def test_resizes_bb_suffix(self):
        url = "https://example.com/art/100x100bb.jpg"
        result = resize_cover_art_url(url, 1200)
        assert "1200x1200bb" in result

    def test_handles_url_without_dimensions(self):
        url = "https://example.com/image.jpg"
        result = resize_cover_art_url(url, 600)
        # Should return unchanged since no dimension pattern found
        assert result == url

    def test_does_not_mangle_other_dimension_patterns(self):
        url = "https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/12x34/400x400bb.jpg"
        result = resize_cover_art_url(url, 600)
        # Should only replace the 400x400bb part, not 12x34
        assert "12x34" in result
        assert "600x600bb" in result


class TestSearchItunesArtwork:
    """Tests for search_itunes_artwork."""

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_resized_url(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"resultCount": 1, "results": [{"artworkUrl100": "https://example.com/art/100x100bb.jpg"}]}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = search_itunes_artwork("Daft Punk", "Around the World", 600)

        assert result is not None
        assert "600x600bb" in result

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_none_on_no_results(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"resultCount": 0, "results": []}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = search_itunes_artwork("Unknown", "Track")
        assert result is None

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_none_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Network error")

        result = search_itunes_artwork("Artist", "Title")
        assert result is None


class TestCreateChapterImage:
    """Tests for create_chapter_image."""

    def test_creates_image_with_artwork(self):
        artwork = _make_test_image()
        result = create_chapter_image(artwork, "Daft Punk", "Around the World")

        assert isinstance(result, bytes)
        assert len(result) > 0
        assert len(result) <= MAX_IMAGE_BYTES

        # Verify it's a valid JPEG
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"
        assert img.size == (CHAPTER_IMAGE_SIZE, CHAPTER_IMAGE_SIZE)

    def test_creates_image_without_artwork(self):
        result = create_chapter_image(None, "Artist", "Title")

        assert isinstance(result, bytes)
        assert len(result) > 0

        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"
        assert img.size == (CHAPTER_IMAGE_SIZE, CHAPTER_IMAGE_SIZE)

    def test_creates_image_with_custom_size(self):
        result = create_chapter_image(None, "Artist", "Title", size=300)

        img = Image.open(io.BytesIO(result))
        assert img.size == (300, 300)

    def test_handles_long_text(self):
        long_artist = "A" * 200
        long_title = "T" * 200
        result = create_chapter_image(None, long_artist, long_title)

        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_handles_empty_text(self):
        result = create_chapter_image(None, "", "")
        assert isinstance(result, bytes)

    def test_handles_corrupt_artwork(self):
        result = create_chapter_image(b"not an image", "Artist", "Title")

        assert isinstance(result, bytes)
        img = Image.open(io.BytesIO(result))
        assert img.size == (CHAPTER_IMAGE_SIZE, CHAPTER_IMAGE_SIZE)


class TestCreateFallbackBackground:
    """Tests for _create_fallback_background."""

    def test_creates_correct_size(self):
        img = _create_fallback_background(600)
        assert img.size == (600, 600)

    def test_creates_rgba_image(self):
        img = _create_fallback_background(100)
        assert img.mode == "RGBA"


class TestCompressToJpeg:
    """Tests for _compress_to_jpeg."""

    def test_stays_under_max_bytes(self):
        img = Image.new("RGB", (600, 600), (128, 64, 200))
        result = _compress_to_jpeg(img, max_bytes=50_000)
        assert len(result) <= 50_000

    def test_returns_valid_jpeg(self):
        img = Image.new("RGB", (600, 600), (0, 0, 0))
        result = _compress_to_jpeg(img)

        loaded = Image.open(io.BytesIO(result))
        assert loaded.format == "JPEG"


class TestDrawTextFitted:
    """Tests for _draw_text_fitted."""

    def test_draws_short_text(self):
        img = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default(size=16)
        # Should not raise
        _draw_text_fitted(draw, 10, 10, "Short", font, 280, (255, 255, 255))

    def test_truncates_long_text(self):
        img = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default(size=16)
        # Very long text that won't fit
        _draw_text_fitted(draw, 10, 10, "A" * 500, font, 100, (255, 255, 255))
        # Should not raise


class TestCleanQuery:
    """Tests for _clean_query."""

    def test_strips_parenthetical_remix(self):
        assert _clean_query("Track Name (Original Mix)") == "Track Name"

    def test_strips_bracket_edit(self):
        assert _clean_query("Title [Radio Edit]") == "Title"

    def test_strips_featuring_feat_dot(self):
        assert _clean_query("Artist feat. Someone") == "Artist"

    def test_strips_featuring_ft(self):
        assert _clean_query("Artist ft Someone Else") == "Artist"

    def test_strips_featuring_full_word(self):
        assert _clean_query("Artist featuring Another") == "Artist"

    def test_strips_multiple_tags(self):
        assert _clean_query("Track (Remix) [Extended]") == "Track"

    def test_leaves_clean_text_unchanged(self):
        assert _clean_query("Clean Title") == "Clean Title"

    def test_handles_empty_string(self):
        assert _clean_query("") == ""


class TestSearchDeezerArtwork:
    """Tests for search_deezer_artwork."""

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_artwork_url(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"data": [{"album": {"cover_xl": '
            b'"https://e-cdns-images.dzcdn.net/images/cover/abc/1000x1000-000000-80-0-0.jpg"}}]}'
        )
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = search_deezer_artwork("Daft Punk", "One More Time", 600)

        assert result is not None
        assert "600x600" in result

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_falls_back_to_cover_big(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"data": [{"album": {"cover_big": '
            b'"https://e-cdns-images.dzcdn.net/images/cover/abc/500x500-000000-80-0-0.jpg"}}]}'
        )
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = search_deezer_artwork("Artist", "Title", 600)

        assert result is not None
        assert "600x600" in result

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_none_on_empty_results(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"data": []}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = search_deezer_artwork("Unknown", "Track")
        assert result is None

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_none_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Network error")

        result = search_deezer_artwork("Artist", "Title")
        assert result is None


class TestSearchMusicbrainzArtwork:
    """Tests for search_musicbrainz_artwork."""

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_cover_art_url(self, mock_urlopen):
        # First call: MusicBrainz recording search
        mb_response = MagicMock()
        mb_response.read.return_value = b'{"recordings": [{"releases": [{"id": "abc-123"}]}]}'
        mb_response.__enter__ = lambda s: s
        mb_response.__exit__ = MagicMock(return_value=False)

        # Second call: Cover Art Archive redirect
        caa_response = MagicMock()
        caa_response.url = "https://archive.org/download/mbid-abc-123/front-500.jpg"
        caa_response.__enter__ = lambda s: s
        caa_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [mb_response, caa_response]

        result = search_musicbrainz_artwork("Daft Punk", "One More Time")

        assert result == "https://archive.org/download/mbid-abc-123/front-500.jpg"
        assert mock_urlopen.call_count == 2

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_none_on_no_recordings(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"recordings": []}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = search_musicbrainz_artwork("Unknown", "Track")
        assert result is None

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_none_on_no_releases(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"recordings": [{"releases": []}]}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = search_musicbrainz_artwork("Artist", "Title")
        assert result is None

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_none_on_mb_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Network error")

        result = search_musicbrainz_artwork("Artist", "Title")
        assert result is None

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_returns_none_on_caa_error(self, mock_urlopen):
        # MusicBrainz succeeds
        mb_response = MagicMock()
        mb_response.read.return_value = b'{"recordings": [{"releases": [{"id": "abc-123"}]}]}'
        mb_response.__enter__ = lambda s: s
        mb_response.__exit__ = MagicMock(return_value=False)

        # Cover Art Archive fails
        mock_urlopen.side_effect = [mb_response, Exception("404 Not Found")]

        result = search_musicbrainz_artwork("Artist", "Title")
        assert result is None


class TestFetchArtworkWaterfall:
    """Tests for fetch_artwork strategy waterfall."""

    @patch("setlist_maker.artwork.download_image")
    def test_tries_shazam_resized_first(self, mock_download):
        mock_download.return_value = b"image-data"

        result = fetch_artwork(
            "Artist", "Title", coverart_url="https://cdn.shazam.com/400x400bb.jpg"
        )

        assert result == b"image-data"
        # Should have been called with resized URL
        call_url = mock_download.call_args[0][0]
        assert "600x600bb" in call_url

    @patch("setlist_maker.artwork.download_image")
    def test_falls_back_to_shazam_original(self, mock_download):
        original_url = "https://cdn.shazam.com/art.jpg"
        mock_download.side_effect = [None, b"image-data"]

        result = fetch_artwork("Artist", "Title", coverart_url=original_url)

        assert result == b"image-data"
        assert mock_download.call_count == 2

    @patch("setlist_maker.artwork.search_itunes_artwork")
    @patch("setlist_maker.artwork.download_image")
    def test_falls_back_to_itunes(self, mock_download, mock_itunes):
        mock_itunes.return_value = "https://itunes.example.com/art.jpg"
        mock_download.side_effect = [None, None, b"image-data"]

        result = fetch_artwork("Artist", "Title", coverart_url="https://cdn.shazam.com/art.jpg")

        assert result == b"image-data"
        mock_itunes.assert_called_once()

    @patch("setlist_maker.artwork.search_itunes_artwork")
    @patch("setlist_maker.artwork.download_image")
    def test_tries_cleaned_itunes_when_different(self, mock_download, mock_itunes):
        mock_itunes.side_effect = [None, "https://itunes.example.com/cleaned.jpg"]
        mock_download.side_effect = [b"image-data"]

        result = fetch_artwork("Artist feat. Someone", "Title (Original Mix)")

        assert result == b"image-data"
        assert mock_itunes.call_count == 2
        # Second call should use cleaned query
        second_call_args = mock_itunes.call_args_list[1]
        assert "feat." not in second_call_args[0][0]
        assert "Original Mix" not in second_call_args[0][1]

    @patch("setlist_maker.artwork.search_itunes_artwork")
    @patch("setlist_maker.artwork.download_image")
    def test_skips_cleaned_itunes_when_same(self, mock_download, mock_itunes):
        mock_itunes.return_value = None
        mock_download.return_value = None

        with (
            patch("setlist_maker.artwork.search_deezer_artwork", return_value=None),
            patch("setlist_maker.artwork.search_musicbrainz_artwork", return_value=None),
        ):
            result = fetch_artwork("Clean Artist", "Clean Title")

        assert result is None
        # iTunes should only be called once since clean == original
        mock_itunes.assert_called_once()

    @patch("setlist_maker.artwork.search_deezer_artwork")
    @patch("setlist_maker.artwork.search_itunes_artwork")
    @patch("setlist_maker.artwork.download_image")
    def test_falls_back_to_deezer(self, mock_download, mock_itunes, mock_deezer):
        mock_itunes.return_value = None
        mock_deezer.return_value = "https://deezer.example.com/art.jpg"
        mock_download.side_effect = [b"image-data"]

        result = fetch_artwork("Artist", "Title")

        assert result == b"image-data"
        mock_deezer.assert_called_once()

    @patch("setlist_maker.artwork.search_musicbrainz_artwork")
    @patch("setlist_maker.artwork.search_deezer_artwork")
    @patch("setlist_maker.artwork.search_itunes_artwork")
    @patch("setlist_maker.artwork.download_image")
    def test_falls_back_to_musicbrainz(self, mock_download, mock_itunes, mock_deezer, mock_mb):
        mock_itunes.return_value = None
        mock_deezer.return_value = None
        mock_mb.return_value = "https://archive.org/art.jpg"
        mock_download.side_effect = [b"image-data"]

        result = fetch_artwork("Artist", "Title")

        assert result == b"image-data"
        mock_mb.assert_called_once()

    @patch("setlist_maker.artwork.search_musicbrainz_artwork")
    @patch("setlist_maker.artwork.search_deezer_artwork")
    @patch("setlist_maker.artwork.search_itunes_artwork")
    @patch("setlist_maker.artwork.download_image")
    def test_returns_none_when_all_fail(self, mock_download, mock_itunes, mock_deezer, mock_mb):
        mock_itunes.return_value = None
        mock_deezer.return_value = None
        mock_mb.return_value = None
        mock_download.return_value = None

        result = fetch_artwork("Artist", "Title")

        assert result is None
