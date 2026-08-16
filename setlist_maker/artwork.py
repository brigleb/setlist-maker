"""
Artwork fetching and chapter image generation.

Provides functionality for:
    - Downloading cover art from URLs (Shazam CDN)
    - Searching iTunes, Deezer, and MusicBrainz as fallbacks for cover art
    - Enumerating every source's offer at once, for the editor's artwork picker
    - Generating MTV-style lower-third overlay images for chapter markers
"""

import io
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


class CoverImageError(Exception):
    """A user-supplied cover image could not be read."""


@dataclass(frozen=True)
class ArtworkCandidate:
    """One cover-art option, tagged with the source that offered it.

    ``label`` carries the album or release name when the API supplies one,
    which is usually the only thing distinguishing two candidates from the
    same source (the single, the album, the compilation).
    """

    source: str
    url: str
    label: str = ""


# Target size for chapter artwork (square, pixels)
CHAPTER_IMAGE_SIZE = 600

# Maximum JPEG file size for embedded artwork (bytes)
MAX_IMAGE_BYTES = 200_000

# JPEG quality to start with when compressing
JPEG_INITIAL_QUALITY = 90

# How many search results each source contributes to the artwork picker.
# Deliberately small: MusicBrainz costs one extra Cover Art Archive request per
# candidate, and near-duplicates collapse anyway, so three per source is enough
# to get the usual single / album / compilation spread without a fetch storm.
CANDIDATES_PER_SOURCE = 3

# Schemes an artwork URL may use. urlopen's default opener also handles file:,
# ftp: and data:, and artwork URLs are no longer only ours -- the editor's
# picker lets a user paste one, and it is persisted to the JSON sidecar and
# fetched by this process on every later run (#20).
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def is_fetchable_url(url: str) -> bool:
    """True for a URL this module is willing to download: http(s), with a host."""
    parsed = urllib.parse.urlparse(url or "")
    return parsed.scheme in _ALLOWED_URL_SCHEMES and bool(parsed.netloc)


def download_image(url: str, timeout: int = 15) -> bytes | None:
    """
    Download an image from a URL.

    Args:
        url: The image URL.
        timeout: Request timeout in seconds.

    Returns:
        Raw image bytes, or None if download failed or the URL is not http(s).
    """
    if not is_fetchable_url(url):
        # Refusing here rather than at the UI covers every caller, including a
        # URL that reached the sidecar by some other route.
        logger.debug("Refusing to download non-HTTP artwork URL: %s", url)
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "setlist-maker/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except Exception as e:
        logger.debug("Failed to download image from %s: %s", url, e)
        return None


def _dedupe(candidates: list[ArtworkCandidate]) -> list[ArtworkCandidate]:
    """Drop repeats by URL, keeping first-seen order (and so source priority)."""
    seen: set[str] = set()
    unique: list[ArtworkCandidate] = []
    for candidate in candidates:
        if candidate.url not in seen:
            seen.add(candidate.url)
            unique.append(candidate)
    return unique


def search_itunes_artwork(artist: str, title: str, size: int = 600) -> str | None:
    """
    Search the iTunes API for album artwork.

    Args:
        artist: Artist name.
        title: Track title.
        size: Desired image size in pixels.

    Returns:
        Artwork URL at the requested size, or None if not found.
    """
    found = itunes_artwork_candidates(artist, title, size, limit=1)
    return found[0].url if found else None


def itunes_artwork_candidates(
    artist: str, title: str, size: int = 600, limit: int = CANDIDATES_PER_SOURCE
) -> list[ArtworkCandidate]:
    """
    Search the iTunes API and return up to ``limit`` distinct cover-art options.

    Still one request whatever ``limit`` is -- the API's own limit parameter
    does the work -- so offering alternates from iTunes costs nothing beyond
    the lookup the waterfall already makes.

    Args:
        artist: Artist name.
        title: Track title.
        size: Desired image size in pixels.
        limit: Maximum number of search results to consider.

    Returns:
        Candidates in iTunes' own relevance order; empty on any failure.
    """
    search_term = f"{artist} {title}"
    params = urllib.parse.urlencode(
        {
            "term": search_term,
            "entity": "song",
            "limit": str(limit),
        }
    )
    url = f"https://itunes.apple.com/search?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "setlist-maker/1.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read())
    except Exception as e:
        logger.debug("iTunes artwork search failed for '%s %s': %s", artist, title, e)
        return []

    found = []
    for result in (data.get("results") or [])[:limit]:
        artwork_url = result.get("artworkUrl100", "")
        if artwork_url:
            found.append(
                ArtworkCandidate(
                    source="iTunes",
                    url=artwork_url.replace("100x100bb", f"{size}x{size}bb"),
                    label=result.get("collectionName") or "",
                )
            )
    return _dedupe(found)


def resize_cover_art_url(url: str, size: int = 600) -> str:
    """
    Resize a Shazam/Apple CDN cover art URL to the desired dimensions.

    Shazam URLs typically contain dimension strings like '400x400' that
    can be swapped for other sizes.

    Args:
        url: Original cover art URL.
        size: Desired size in pixels.

    Returns:
        URL with updated dimensions.
    """
    return re.sub(r"\d+x\d+(?=bb|cc)", f"{size}x{size}", url)


def _clean_query(text: str) -> str:
    """
    Strip remix tags, featuring info, and parenthetical/bracket noise from a string.

    Examples:
        "Track Name (Original Mix)" → "Track Name"
        "Artist feat. Someone" → "Artist"
        "Title [Radio Edit]" → "Title"
    """
    # Remove parenthetical and bracketed suffixes: (Original Mix), [Radio Edit], etc.
    cleaned = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", text)
    # Remove featuring info: feat., ft., featuring
    cleaned = re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def search_deezer_artwork(artist: str, title: str, size: int = 600) -> str | None:
    """
    Search the Deezer API for album artwork.

    Args:
        artist: Artist name.
        title: Track title.
        size: Desired image size in pixels.

    Returns:
        Artwork URL at the requested size, or None if not found.
    """
    found = deezer_artwork_candidates(artist, title, size, limit=1)
    return found[0].url if found else None


def _deezer_search(query: str) -> list[dict] | None:
    """Run one Deezer search.

    Returns the result rows, ``[]`` for a successful search that matched
    nothing, and ``None`` when the request itself failed -- a distinction the
    caller needs, because retrying a different *query* is only worth doing when
    Deezer actually answered.
    """
    params = urllib.parse.urlencode({"q": query})
    url = f"https://api.deezer.com/search?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "setlist-maker/1.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read())
    except Exception as e:
        logger.debug("Deezer artwork search failed for %r: %s", query, e)
        return None
    return data.get("data") or []


def deezer_artwork_candidates(
    artist: str, title: str, size: int = 600, limit: int = CANDIDATES_PER_SOURCE
) -> list[ArtworkCandidate]:
    """
    Search the Deezer API and return up to ``limit`` distinct cover-art options.

    Two queries at most. Deezer's advanced syntax (``artist:"..." track:"..."``)
    demands an exact field match and comes back empty for a large share of real
    tracks -- measured returning 0 rows for both "Daft Punk / One More Time" and
    "Kraftwerk / Autobahn" while the plain term query returns 48 and 164 -- so a
    plain-term retry runs when the advanced one finds nothing. Without it Deezer
    contributes nothing to the picker *or* to the waterfall for those tracks.

    Args:
        artist: Artist name.
        title: Track title.
        size: Desired image size in pixels.
        limit: Maximum number of search results to consider.

    Returns:
        Candidates in Deezer's own relevance order; empty on any failure.
    """
    rows: list[dict] = []
    for query in (f'artist:"{artist}" track:"{title}"', f"{artist} {title}"):
        result = _deezer_search(query)
        if result is None:
            break  # Deezer is unreachable; a second query costs another timeout for nothing
        if result:
            rows = result
            break

    found = []
    for row in rows[:limit]:
        album = row.get("album") or {}
        # Prefer cover_xl (1000x1000), fall back to cover_big (500x500)
        artwork_url = album.get("cover_xl") or album.get("cover_big")
        if artwork_url:
            found.append(
                ArtworkCandidate(
                    source="Deezer",
                    # Deezer URLs use /{dim}x{dim}- pattern for resizing
                    url=re.sub(r"/\d+x\d+-", f"/{size}x{size}-", artwork_url),
                    label=album.get("title") or "",
                )
            )
    return _dedupe(found)


def search_musicbrainz_artwork(artist: str, title: str) -> str | None:
    """
    Search MusicBrainz for a recording and fetch its cover art from Cover Art Archive.

    Two-step lookup:
        1. Search MusicBrainz for the recording to get a release ID.
        2. Request the front cover from Cover Art Archive.

    Args:
        artist: Artist name.
        title: Track title.

    Returns:
        Cover art image URL, or None if not found.
    """
    found = musicbrainz_artwork_candidates(artist, title, limit=1)
    return found[0].url if found else None


def musicbrainz_artwork_candidates(
    artist: str, title: str, limit: int = CANDIDATES_PER_SOURCE
) -> list[ArtworkCandidate]:
    """
    Search MusicBrainz and return up to ``limit`` Cover Art Archive options.

    The most expensive source in the picker, and the only one whose cost grows
    with ``limit``: one recording search, then one Cover Art Archive request per
    candidate release, because CAA only reveals whether a release *has* a front
    cover by being asked for it. Releases without one are skipped rather than
    offered as tiles that would fail to load.

    Args:
        artist: Artist name.
        title: Track title.
        limit: Maximum number of releases to look up.

    Returns:
        Candidates in MusicBrainz' own relevance order; empty on any failure.
    """
    # Step 1: Search MusicBrainz for the recording
    mb_query = f'artist:"{artist}" AND recording:"{title}"'
    params = urllib.parse.urlencode({"query": mb_query, "fmt": "json", "limit": str(limit)})
    mb_url = f"https://musicbrainz.org/ws/2/recording?{params}"

    headers = {
        "User-Agent": "setlist-maker/1.0 (https://github.com/brigleb/setlist-maker)",
        "Accept": "application/json",
    }

    try:
        req = urllib.request.Request(mb_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read())
    except Exception as e:
        logger.debug("MusicBrainz search failed for '%s %s': %s", artist, title, e)
        return []

    # Distinct releases, best match first; several recordings can share one.
    releases: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for recording in data.get("recordings") or []:
        for release in recording.get("releases") or []:
            release_id = release.get("id")
            if release_id and release_id not in seen_ids:
                seen_ids.add(release_id)
                releases.append((release_id, release.get("title") or ""))
            if len(releases) >= limit:
                break
        if len(releases) >= limit:
            break

    # Step 2: Get each release's front cover from Cover Art Archive
    found = []
    for release_id, release_title in releases:
        caa_url = f"https://coverartarchive.org/release/{release_id}/front-500"
        try:
            req = urllib.request.Request(caa_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                # Cover Art Archive redirects to the actual image URL
                found.append(
                    ArtworkCandidate(
                        source="Cover Art Archive", url=response.url, label=release_title
                    )
                )
        except Exception as e:
            logger.debug("Cover Art Archive lookup failed for release %s: %s", release_id, e)
    return _dedupe(found)


def artwork_candidates(
    artist: str,
    title: str,
    size: int = CHAPTER_IMAGE_SIZE,
    per_source: int = CANDIDATES_PER_SOURCE,
) -> list[ArtworkCandidate]:
    """
    Gather every cover-art option the search sources offer for one track.

    ``fetch_artwork`` walks the same sources but stops at the first that yields
    an image -- right for an unattended run, and the reason there is nothing to
    compare when it picks wrong. This asks all of them, so the editor can show
    the alternates and let the user choose (#20). Sources are queried in the
    waterfall's own order, so the option already in use normally sorts first.

    The track's saved ``coverart_url`` is deliberately absent: it is one
    particular *answer*, not a source, and the caller knows whether it wants it
    shown alongside.

    Args:
        artist: Artist name.
        title: Track title.
        size: Desired image size in pixels.
        per_source: Maximum candidates to take from each source.

    Returns:
        Deduplicated candidates; empty if every source came back with nothing.
    """
    found: list[ArtworkCandidate] = []
    found += itunes_artwork_candidates(artist, title, size, per_source)

    cleaned_artist = _clean_query(artist)
    cleaned_title = _clean_query(title)
    if cleaned_artist != artist or cleaned_title != title:
        found += itunes_artwork_candidates(cleaned_artist, cleaned_title, size, per_source)

    found += deezer_artwork_candidates(artist, title, size, per_source)
    found += musicbrainz_artwork_candidates(artist, title, per_source)
    return _dedupe(found)


def fetch_artwork(
    artist: str,
    title: str,
    coverart_url: str | None = None,
    size: int = CHAPTER_IMAGE_SIZE,
) -> bytes | None:
    """
    Fetch cover art for a track using a waterfall of sources.

    Tries in order: Shazam CDN (resized), Shazam CDN (original), iTunes exact,
    iTunes cleaned query (if different), Deezer, MusicBrainz + Cover Art Archive.

    Args:
        artist: Artist name.
        title: Track title.
        coverart_url: Pre-saved cover art URL (from Shazam).
        size: Desired image size in pixels.

    Returns:
        Raw image bytes, or None if not found.
    """
    # Build the strategy list: (description, url_fetcher) pairs
    strategies: list[tuple[str, callable]] = []

    if coverart_url:
        resized_url = resize_cover_art_url(coverart_url, size)
        strategies.append(("Shazam CDN (resized)", lambda: resized_url))
        strategies.append(("Shazam CDN (original)", lambda: coverart_url))

    strategies.append(("iTunes exact", lambda: search_itunes_artwork(artist, title, size)))

    # Only add cleaned iTunes query if it differs from the original
    cleaned_artist = _clean_query(artist)
    cleaned_title = _clean_query(title)
    if cleaned_artist != artist or cleaned_title != title:
        strategies.append(
            (
                "iTunes cleaned",
                lambda: search_itunes_artwork(cleaned_artist, cleaned_title, size),
            )
        )

    strategies.append(("Deezer", lambda: search_deezer_artwork(artist, title, size)))
    strategies.append(
        (
            "MusicBrainz",
            lambda: search_musicbrainz_artwork(artist, title),
        )
    )

    for description, get_url in strategies:
        url = get_url()
        if url:
            data = download_image(url)
            if data:
                logger.debug("Artwork found via %s for '%s - %s'", description, artist, title)
                return data
            logger.debug("Artwork download failed via %s for '%s - %s'", description, artist, title)

    logger.debug("No artwork found for '%s - %s'", artist, title)
    return None


def _find_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Find a usable bold sans-serif font on the system.

    Tries common font paths across macOS, Linux, and Windows.
    Falls back to Pillow's built-in default font.

    Args:
        size: Desired font size in points.

    Returns:
        A Pillow font object.
    """
    # Common bold sans-serif fonts to try, in preference order
    font_candidates = [
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/Library/Fonts/Arial.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        # Windows
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]

    for font_path in font_candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except (OSError, IOError):
            continue

    # Try by name (works on some systems)
    for name in ["DejaVuSans-Bold", "DejaVuSans", "Arial", "Helvetica"]:
        try:
            return ImageFont.truetype(name, size=size)
        except (OSError, IOError):
            continue

    # Last resort: Pillow's built-in font
    return ImageFont.load_default(size=size)


def create_chapter_image(
    artwork_bytes: bytes | None,
    artist: str,
    title: str,
    size: int = CHAPTER_IMAGE_SIZE,
) -> bytes:
    """
    Create an MTV-style chapter image with a lower-third text overlay.

    If artwork_bytes is provided, it is used as the background. Otherwise,
    a dark gradient background is generated.

    Args:
        artwork_bytes: Raw image data for the cover art background.
        artist: Artist name to display.
        title: Track title to display.
        size: Output image dimensions (square).

    Returns:
        JPEG image bytes, optimized to stay under MAX_IMAGE_BYTES.
    """
    # Load or create background
    if artwork_bytes:
        try:
            base = Image.open(io.BytesIO(artwork_bytes)).convert("RGBA")
            base = base.resize((size, size), Image.LANCZOS)
        except Exception as e:
            logger.debug("Failed to load artwork image, using fallback: %s", e)
            base = _create_fallback_background(size)
    else:
        base = _create_fallback_background(size)

    # Create transparent overlay for the lower-third bar
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Draw semi-transparent lower-third bar (bottom ~28% of image)
    bar_top = int(size * 0.72)
    draw.rectangle([(0, bar_top), (size, size)], fill=(0, 0, 0, 170))

    # Load fonts
    title_font_size = max(size // 18, 16)
    artist_font_size = max(size // 22, 13)
    title_font = _find_font(title_font_size)
    artist_font = _find_font(artist_font_size)

    # Text positioning
    padding = size // 30
    text_x = padding
    title_y = bar_top + padding

    # Draw title (white, larger)
    _draw_text_fitted(draw, text_x, title_y, title, title_font, size - 2 * padding, (255, 255, 255))

    # Draw artist below title (lighter gray, smaller)
    artist_y = title_y + title_font_size + padding // 2
    _draw_text_fitted(
        draw, text_x, artist_y, artist, artist_font, size - 2 * padding, (200, 200, 200)
    )

    # Composite and convert to RGB for JPEG
    result = Image.alpha_composite(base, overlay).convert("RGB")

    return _compress_to_jpeg(result)


def _draw_text_fitted(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    fill: tuple[int, ...],
) -> None:
    """Draw text, truncating with ellipsis if it exceeds max_width."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]

    if text_width <= max_width:
        draw.text((x, y), text, font=font, fill=fill)
        return

    # Truncate with ellipsis
    for end in range(len(text) - 1, 0, -1):
        truncated = text[:end] + "..."
        bbox = draw.textbbox((0, 0), truncated, font=font)
        if bbox[2] - bbox[0] <= max_width:
            draw.text((x, y), truncated, font=font, fill=fill)
            return

    draw.text((x, y), text[:3] + "...", font=font, fill=fill)


def _create_fallback_background(size: int) -> Image.Image:
    """Create a dark gradient background when no artwork is available."""
    img = Image.new("RGBA", (size, size), (30, 30, 40, 255))
    draw = ImageDraw.Draw(img)
    # Simple vertical gradient from dark blue-gray to darker
    for y in range(size):
        ratio = y / size
        r = int(30 + 15 * ratio)
        g = int(30 + 10 * ratio)
        b = int(40 + 20 * ratio)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))
    return img


def load_cover_image(path: Path, size: int = CHAPTER_IMAGE_SIZE) -> bytes:
    """Load a user-supplied episode cover, normalized for ID3 embedding.

    Center-crops to square (a no-op on already-square art) and resizes to
    ``size``, then compresses like every other embedded image.

    Deliberately skips ``create_chapter_image``'s lower-third overlay: a cover
    the user hand-picked is finished art, not a generated chapter card. Per-track
    chapter images are unaffected and keep their overlays.

    Args:
        path: Image file to use as the episode cover.
        size: Output dimensions (square).

    Returns:
        JPEG bytes, under MAX_IMAGE_BYTES.

    Raises:
        CoverImageError: The file is missing, unreadable, or not an image.
    """
    try:
        with Image.open(path) as opened:
            base = opened.convert("RGB")
    except (OSError, ValueError) as e:
        raise CoverImageError(f"Could not read cover image '{path}': {e}") from e

    width, height = base.size
    if width != height:
        edge = min(width, height)
        left = (width - edge) // 2
        top = (height - edge) // 2
        base = base.crop((left, top, left + edge, top + edge))
        logger.debug("Center-cropped cover from %dx%d to %dx%d", width, height, edge, edge)

    if base.size != (size, size):
        base = base.resize((size, size), Image.LANCZOS)

    return _compress_to_jpeg(base)


def _compress_to_jpeg(image: Image.Image, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    """Compress an image to JPEG, reducing quality until it fits under max_bytes."""
    quality = JPEG_INITIAL_QUALITY
    while quality >= 30:
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
        quality -= 10

    # If still too large, reduce dimensions
    smaller = image.resize((400, 400), Image.LANCZOS)
    buf = io.BytesIO()
    smaller.save(buf, format="JPEG", quality=60, optimize=True)
    return buf.getvalue()
