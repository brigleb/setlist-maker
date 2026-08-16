"""Tests for setlist_maker.artwork module."""

import io
import json
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw, ImageFont

from setlist_maker.artwork import (
    CHAPTER_IMAGE_SIZE,
    MAX_IMAGE_BYTES,
    ArtworkCandidate,
    _clean_query,
    _compress_to_jpeg,
    _create_fallback_background,
    _draw_text_fitted,
    artwork_candidates,
    create_chapter_image,
    deezer_artwork_candidates,
    download_image,
    fetch_artwork,
    is_fetchable_url,
    itunes_artwork_candidates,
    musicbrainz_artwork_candidates,
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


def _json_response(payload: dict) -> MagicMock:
    """A urlopen context-manager stub that hands back ``payload`` as JSON."""
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    return response


def _raw_response(body: bytes) -> MagicMock:
    """A urlopen stub returning an exact response body, junk included."""
    response = MagicMock()
    response.read.return_value = body
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    return response


def _redirect_response(url: str) -> MagicMock:
    """A urlopen stub standing in for a Cover Art Archive redirect."""
    response = MagicMock()
    response.url = url
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    return response


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


class TestLoadCoverImage:
    """Tests for user-supplied episode cover images (--cover)."""

    @staticmethod
    def _write(path, size, color=(200, 30, 30)):
        from PIL import Image

        Image.new("RGB", size, color).save(path, format="JPEG")
        return path

    def test_square_cover_is_normalized_to_chapter_size(self, tmp_path):
        from setlist_maker.artwork import CHAPTER_IMAGE_SIZE, load_cover_image

        src = self._write(tmp_path / "cover.jpg", (1080, 1080))
        data = load_cover_image(src)

        from io import BytesIO

        from PIL import Image

        out = Image.open(BytesIO(data))
        assert out.size == (CHAPTER_IMAGE_SIZE, CHAPTER_IMAGE_SIZE)
        assert out.format == "JPEG"

    def test_wide_cover_is_center_cropped_not_squashed(self, tmp_path):
        """A 2:1 source must crop to square; squashing would distort the art.

        The two are only distinguishable by content that a crop *discards*.
        Colored bands at the far edges survive a squash (compressed inward) and
        vanish under a center crop, so they are what this asserts on -- an
        earlier version compared left/right halves, which a squash preserves
        just as faithfully, and passed against a squashing implementation.
        """
        from io import BytesIO

        from PIL import Image

        from setlist_maker.artwork import load_cover_image

        # 1200x600 red, with green bands in the outer 150px at each edge.
        # A center crop keeps x=300..900, discarding both bands entirely.
        img = Image.new("RGB", (1200, 600), (200, 30, 30))
        band = Image.new("RGB", (150, 600), (0, 220, 0))
        img.paste(band, (0, 0))
        img.paste(band, (1050, 0))
        src = tmp_path / "wide.jpg"
        img.save(src, format="JPEG")

        out = Image.open(BytesIO(load_cover_image(src))).convert("RGB")
        assert out.size[0] == out.size[1]
        for x in (5, out.size[0] - 6):
            pixel = out.getpixel((x, out.size[1] // 2))
            assert pixel[0] > pixel[1], (
                f"edge pixel {pixel} at x={x} is still green -- the source was squashed "
                f"into a square instead of center-cropped"
            )

    def test_no_lower_third_overlay_is_drawn(self, tmp_path):
        """A hand-picked cover is finished art -- it must not get a text bar."""
        from io import BytesIO

        from PIL import Image

        from setlist_maker.artwork import create_chapter_image, load_cover_image

        src = self._write(tmp_path / "cover.jpg", (600, 600), color=(220, 40, 40))
        plain = Image.open(BytesIO(load_cover_image(src)))
        overlaid = Image.open(BytesIO(create_chapter_image(src.read_bytes(), "A", "T")))

        # Sample inside the lower-third bar create_chapter_image draws at 72%
        y = int(plain.size[1] * 0.85)
        assert plain.getpixel((plain.size[0] // 2, y))[0] > 150  # still bright red
        assert overlaid.getpixel((overlaid.size[0] // 2, y))[0] < 100  # darkened by the bar

    def test_missing_file_raises_cover_image_error(self, tmp_path):
        from setlist_maker.artwork import CoverImageError, load_cover_image

        with pytest.raises(CoverImageError):
            load_cover_image(tmp_path / "nope.jpg")

    def test_non_image_raises_cover_image_error(self, tmp_path):
        from setlist_maker.artwork import CoverImageError, load_cover_image

        bogus = tmp_path / "notanimage.jpg"
        bogus.write_text("this is not an image")
        with pytest.raises(CoverImageError):
            load_cover_image(bogus)


class TestDownloadImageScheme:
    """download_image only speaks http(s) (#20).

    Cover-art URLs used to come only from Shazam and the search APIs. The
    editor's picker lets a user type one, and it is persisted to the JSON
    sidecar and handed to urlopen by this process on every later run --
    whose default opener treats file:, ftp: and data: as perfectly good
    sources of "cover art".
    """

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "data:image/png;base64,iVBORw0KGgo=",
            "ftp://example.com/cover.jpg",
            "javascript:alert(1)",
            "//example.com/cover.jpg",  # scheme-relative: no scheme at all
            "not a url",
            "",
        ],
    )
    def test_refuses_anything_but_http(self, url):
        with patch("setlist_maker.artwork.urllib.request.urlopen") as mock_urlopen:
            assert download_image(url) is None
            mock_urlopen.assert_not_called()

    def test_does_not_read_a_local_file(self, tmp_path):
        """The end-to-end version: a real file: URL, no mock in the way."""
        secret = tmp_path / "secret.txt"
        secret.write_text("this is not cover art")
        assert download_image(secret.as_uri()) is None

    @pytest.mark.parametrize(
        "url,ok",
        [
            ("https://example.com/a.jpg", True),
            ("http://example.com/a.jpg", True),
            ("https:///a.jpg", False),  # no host
            ("HTTPS://example.com/a.jpg", True),  # urlparse lowercases the scheme
        ],
    )
    def test_is_fetchable_url(self, url, ok):
        assert is_fetchable_url(url) is ok


class TestItunesArtworkCandidates:
    """iTunes' contribution to the picker."""

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_offers_one_candidate_per_distinct_album(self, mock_urlopen):
        mock_urlopen.return_value = _json_response(
            {
                "resultCount": 3,
                "results": [
                    {"collectionName": "Discovery", "artworkUrl100": "https://x/a/100x100bb.jpg"},
                    {"collectionName": "Musique", "artworkUrl100": "https://x/b/100x100bb.jpg"},
                    # Same album as the first result: one tile, not two.
                    {"collectionName": "Discovery", "artworkUrl100": "https://x/a/100x100bb.jpg"},
                ],
            }
        )

        found = itunes_artwork_candidates("Daft Punk", "One More Time", 600, limit=3)

        assert [c.url for c in found] == [
            "https://x/a/600x600bb.jpg",
            "https://x/b/600x600bb.jpg",
        ]
        assert [c.source for c in found] == ["iTunes", "iTunes"]
        assert found[0].label == "Discovery"  # what distinguishes two iTunes tiles
        # Still one request whatever the limit: the API's own limit does the work,
        # so widening iTunes for the picker costs nothing the waterfall didn't pay.
        assert mock_urlopen.call_count == 1
        assert "limit=3" in mock_urlopen.call_args[0][0].full_url

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_network_failure_yields_no_candidates(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Network error")
        assert itunes_artwork_candidates("Artist", "Title") == []


class TestDeezerArtworkCandidates:
    """Deezer's contribution, including the query that returned nothing."""

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_retries_with_a_plain_term_when_the_advanced_query_is_empty(self, mock_urlopen):
        """Deezer's advanced syntax demands an exact field match and misses a
        large share of real tracks -- measured returning 0 rows for both
        'Daft Punk / One More Time' and 'Kraftwerk / Autobahn' while the plain
        term returns 48 and 164. Without the retry Deezer contributes nothing
        to the picker, and nothing to the waterfall either.
        """
        mock_urlopen.side_effect = [
            _json_response({"total": 0, "data": []}),
            _json_response(
                {
                    "data": [
                        {
                            "album": {
                                "title": "Discovery",
                                "cover_xl": "https://dz/cover/hash/1000x1000-000.jpg",
                            }
                        }
                    ]
                }
            ),
        ]

        found = deezer_artwork_candidates("Daft Punk", "One More Time", 600, limit=3)

        assert [c.url for c in found] == ["https://dz/cover/hash/600x600-000.jpg"]
        assert found[0].label == "Discovery"
        assert mock_urlopen.call_count == 2
        advanced, plain = (call[0][0].full_url for call in mock_urlopen.call_args_list)
        assert "artist%3A" in advanced  # the exact-field query is still tried first
        assert "artist%3A" not in plain

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_does_not_retry_when_the_advanced_query_answers(self, mock_urlopen):
        mock_urlopen.return_value = _json_response(
            {
                "data": [
                    {
                        "album": {
                            "title": "Dig Your Own Hole",
                            "cover_big": "https://dz/c/500x500-0.jpg",
                        }
                    }
                ]
            }
        )
        found = deezer_artwork_candidates("The Chemical Brothers", "Block Rockin' Beats")
        assert len(found) == 1
        assert mock_urlopen.call_count == 1

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_does_not_retry_when_the_request_itself_failed(self, mock_urlopen):
        """Retrying a different *query* only helps when Deezer answered. After a
        network failure the second query just buys another 15s timeout, so the
        rung costs exactly what it did before this retry existed."""
        mock_urlopen.side_effect = Exception("Network error")
        assert deezer_artwork_candidates("Artist", "Title") == []
        assert mock_urlopen.call_count == 1

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_single_result_helper_gains_the_retry(self, mock_urlopen):
        """search_deezer_artwork is now a wrapper, so the waterfall benefits too."""
        mock_urlopen.side_effect = [
            _json_response({"data": []}),
            _json_response({"data": [{"album": {"cover_big": "https://dz/c/500x500-0.jpg"}}]}),
        ]
        assert search_deezer_artwork("Daft Punk", "One More Time", 600) == (
            "https://dz/c/600x600-0.jpg"
        )


class TestMusicbrainzArtworkCandidates:
    """MusicBrainz + Cover Art Archive: the only source whose cost grows with limit."""

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_offers_one_candidate_per_release_and_skips_coverless_ones(self, mock_urlopen):
        mock_urlopen.side_effect = [
            _json_response(
                {
                    "recordings": [
                        {
                            "releases": [
                                {"id": "r1", "title": "Discovery"},
                                {"id": "r2", "title": "Coverless Comp"},
                            ]
                        }
                    ]
                }
            ),
            _redirect_response("https://archive.org/mbid-r1/front-500.jpg"),
            Exception("404 Not Found"),  # r2 has no front cover in the archive
        ]

        found = musicbrainz_artwork_candidates("Daft Punk", "One More Time", limit=2)

        # A release with no cover is dropped, not offered as a tile that 404s.
        assert [(c.source, c.url, c.label) for c in found] == [
            ("Cover Art Archive", "https://archive.org/mbid-r1/front-500.jpg", "Discovery")
        ]
        assert mock_urlopen.call_count == 3  # one search, one lookup per release

    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_collects_distinct_releases_across_recordings(self, mock_urlopen):
        """Several recordings routinely name the same release; it is one tile."""
        mock_urlopen.side_effect = [
            _json_response(
                {
                    "recordings": [
                        {"releases": [{"id": "shared", "title": "Discovery"}]},
                        {"releases": [{"id": "shared", "title": "Discovery"}]},
                        {"releases": [{"id": "other", "title": "Alive 2007"}]},
                    ]
                }
            ),
            _redirect_response("https://archive.org/mbid-shared/front-500.jpg"),
            _redirect_response("https://archive.org/mbid-other/front-500.jpg"),
        ]

        found = musicbrainz_artwork_candidates("Daft Punk", "One More Time", limit=3)

        assert [c.label for c in found] == ["Discovery", "Alive 2007"]
        assert mock_urlopen.call_count == 3  # two lookups, not three


class TestArtworkCandidates:
    """The whole picker list: every source asked, none skipped."""

    @patch("setlist_maker.artwork.musicbrainz_artwork_candidates")
    @patch("setlist_maker.artwork.deezer_artwork_candidates")
    @patch("setlist_maker.artwork.itunes_artwork_candidates")
    def test_asks_every_source_and_dedupes_across_them(self, mock_itunes, mock_deezer, mock_mb):
        mock_itunes.return_value = [ArtworkCandidate("iTunes", "https://shared/c.jpg", "Discovery")]
        mock_deezer.return_value = [
            ArtworkCandidate("Deezer", "https://shared/c.jpg", "Discovery"),
            ArtworkCandidate("Deezer", "https://dz/other.jpg", "Alive 2007"),
        ]
        mock_mb.return_value = [ArtworkCandidate("Cover Art Archive", "https://caa/r1.jpg", "D")]

        found = artwork_candidates("Daft Punk", "One More Time")

        # The contrast with fetch_artwork: an early answer does not stop the rest.
        assert mock_itunes.called and mock_deezer.called and mock_mb.called
        assert [c.url for c in found] == [
            "https://shared/c.jpg",
            "https://dz/other.jpg",
            "https://caa/r1.jpg",
        ]
        # First-seen wins, so the list keeps the waterfall's own source priority.
        assert found[0].source == "iTunes"

    @patch("setlist_maker.artwork.musicbrainz_artwork_candidates", return_value=[])
    @patch("setlist_maker.artwork.deezer_artwork_candidates", return_value=[])
    @patch("setlist_maker.artwork.itunes_artwork_candidates", return_value=[])
    def test_asks_itunes_twice_when_the_cleaned_query_differs(self, mock_itunes, _dz, _mb):
        artwork_candidates("Daft Punk", "One More Time (Radio Edit)")
        assert mock_itunes.call_count == 2
        assert mock_itunes.call_args_list[1][0][1] == "One More Time"

    @patch("setlist_maker.artwork.musicbrainz_artwork_candidates", return_value=[])
    @patch("setlist_maker.artwork.deezer_artwork_candidates", return_value=[])
    @patch("setlist_maker.artwork.itunes_artwork_candidates", return_value=[])
    def test_asks_itunes_once_when_the_cleaned_query_is_the_same(self, mock_itunes, _dz, _mb):
        artwork_candidates("Daft Punk", "One More Time")
        assert mock_itunes.call_count == 1

    @patch("setlist_maker.artwork.musicbrainz_artwork_candidates", return_value=[])
    @patch("setlist_maker.artwork.deezer_artwork_candidates", return_value=[])
    @patch("setlist_maker.artwork.itunes_artwork_candidates", return_value=[])
    def test_no_source_answering_is_an_empty_list_not_an_error(self, _it, _dz, _mb):
        assert artwork_candidates("Nobody", "Nothing") == []


class TestSourceHelpersTolerateJunkResponses:
    """A 200 carrying JSON of the wrong shape must yield nothing, not raise.

    These are third-party APIs reached from a request handler and from a
    multi-hour identify run: an escaping AttributeError means no HTTP response
    at all in the editor, and a dead run on the CLI. The pre-refactor helpers
    parsed inside their try/except, and the candidate versions must too.
    """

    @pytest.mark.parametrize(
        "body",
        [b"null", b"[1, 2]", b'"a string"', b"42", b'{"results": [1, 2]}', b'{"results": null}'],
    )
    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_itunes(self, mock_urlopen, body):
        mock_urlopen.return_value = _raw_response(body)
        assert itunes_artwork_candidates("A", "T") == []
        assert search_itunes_artwork("A", "T") is None

    @pytest.mark.parametrize(
        "body", [b"null", b"[1, 2]", b'{"data": ["x"]}', b'{"data": [{"album": 7}]}']
    )
    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_deezer(self, mock_urlopen, body):
        mock_urlopen.side_effect = [_raw_response(body), _raw_response(body)]
        assert deezer_artwork_candidates("A", "T") == []

    @pytest.mark.parametrize(
        "body",
        [
            b"null",
            b'"a string"',
            b'{"recordings": [{"releases": ["x"]}]}',
            b'{"recordings": [7]}',
        ],
    )
    @patch("setlist_maker.artwork.urllib.request.urlopen")
    def test_musicbrainz(self, mock_urlopen, body):
        mock_urlopen.return_value = _raw_response(body)
        assert musicbrainz_artwork_candidates("A", "T") == []
        assert search_musicbrainz_artwork("A", "T") is None


class TestIsFetchableUrlEdges:
    @pytest.mark.parametrize("url", ["http://[::1", "https://[bad", "http://[", "https://]["])
    def test_unparseable_authority_is_not_fetchable(self, url):
        """urlparse raises ValueError on a malformed authority. This is reached
        from a request handler, where an escaping exception means the browser
        gets no response at all and a traceback lands in the user's terminal."""
        assert is_fetchable_url(url) is False
        with patch("setlist_maker.artwork.urllib.request.urlopen") as mock_urlopen:
            assert download_image(url) is None
            mock_urlopen.assert_not_called()
