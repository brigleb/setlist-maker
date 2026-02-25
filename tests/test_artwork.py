"""Tests for setlist_maker.artwork module."""

import io
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw, ImageFont

from setlist_maker.artwork import (
    CHAPTER_IMAGE_SIZE,
    MAX_IMAGE_BYTES,
    _compress_to_jpeg,
    _create_fallback_background,
    _draw_text_fitted,
    create_chapter_image,
    resize_cover_art_url,
    search_itunes_artwork,
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
