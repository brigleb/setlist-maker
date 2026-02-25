"""
Artwork fetching and chapter image generation.

Provides functionality for:
    - Downloading cover art from URLs (Shazam CDN)
    - Searching iTunes as a fallback for cover art
    - Generating MTV-style lower-third overlay images for chapter markers
"""

import io
import json
import logging
import re
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Target size for chapter artwork (square, pixels)
CHAPTER_IMAGE_SIZE = 600

# Maximum JPEG file size for embedded artwork (bytes)
MAX_IMAGE_BYTES = 200_000

# JPEG quality to start with when compressing
JPEG_INITIAL_QUALITY = 90


def download_image(url: str, timeout: int = 15) -> bytes | None:
    """
    Download an image from a URL.

    Args:
        url: The image URL.
        timeout: Request timeout in seconds.

    Returns:
        Raw image bytes, or None if download failed.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "setlist-maker/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except Exception as e:
        logger.debug("Failed to download image from %s: %s", url, e)
        return None


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
    search_term = f"{artist} {title}"
    params = urllib.parse.urlencode(
        {
            "term": search_term,
            "entity": "song",
            "limit": "1",
        }
    )
    url = f"https://itunes.apple.com/search?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "setlist-maker/1.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read())

        if data.get("resultCount", 0) > 0:
            result = data["results"][0]
            artwork_url = result.get("artworkUrl100", "")
            if artwork_url:
                return artwork_url.replace("100x100bb", f"{size}x{size}bb")
    except Exception as e:
        logger.debug("iTunes artwork search failed for '%s %s': %s", artist, title, e)

    return None


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


def fetch_artwork(
    artist: str,
    title: str,
    coverart_url: str | None = None,
    size: int = CHAPTER_IMAGE_SIZE,
) -> bytes | None:
    """
    Fetch cover art for a track, trying saved URL first, then iTunes.

    Args:
        artist: Artist name.
        title: Track title.
        coverart_url: Pre-saved cover art URL (from Shazam).
        size: Desired image size in pixels.

    Returns:
        Raw image bytes, or None if not found.
    """
    # Try saved Shazam URL first
    if coverart_url:
        resized_url = resize_cover_art_url(coverart_url, size)
        data = download_image(resized_url)
        if data:
            return data
        # Try original URL if resize didn't work
        data = download_image(coverart_url)
        if data:
            return data

    # Fallback to iTunes Search API
    itunes_url = search_itunes_artwork(artist, title, size)
    if itunes_url:
        data = download_image(itunes_url)
        if data:
            return data

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
