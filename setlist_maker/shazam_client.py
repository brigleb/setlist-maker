"""Shazam recognition with exponential-backoff retry for rate limits."""

import asyncio
import random
from pathlib import Path

from pydub import AudioSegment
from shazamio import Shazam

MAX_RETRIES = 5
INITIAL_BACKOFF = 30


async def identify_sample_with_retry(
    shazam: Shazam, segment: AudioSegment, temp_dir: str, max_retries: int = MAX_RETRIES
) -> dict | None:
    """
    Identify a single audio segment using Shazam with exponential backoff retry.
    Returns track info dict or None if not identified.
    """
    temp_path = str(Path(temp_dir) / "temp_sample.mp3")
    segment.export(temp_path, format="mp3")

    backoff = INITIAL_BACKOFF
    for attempt in range(max_retries):
        try:
            result = await shazam.recognize(temp_path)
            if result and "track" in result:
                track = result["track"]
                images = track.get("images", {})
                return {
                    "title": track.get("title", "Unknown Title"),
                    "artist": track.get("subtitle", "Unknown Artist"),
                    "shazam_url": track.get("url"),
                    "album": track.get("sections", [{}])[0].get("metadata", [{}])[0].get("text")
                    if track.get("sections")
                    else None,
                    "coverart_url": images.get("coverarthq") or images.get("coverart"),
                }
            return None
        except Exception as e:
            error_str = str(e).lower()

            # Check if it's a rate limit error
            if "429" in error_str or "too many" in error_str or "rate" in error_str:
                if attempt < max_retries - 1:
                    # Add jitter to avoid thundering herd
                    jitter = random.uniform(0, backoff * 0.1)
                    wait_time = backoff + jitter
                    print(
                        f"\n  Warning: Rate limited. Backing off for {wait_time:.0f} seconds "
                        f"(attempt {attempt + 1}/{max_retries})..."
                    )
                    await asyncio.sleep(wait_time)
                    backoff *= 2  # Exponential backoff
                else:
                    print(f"\n  Error: Rate limit persisted after {max_retries} attempts")
                    return None
            else:
                # Other error - log and return None
                print(f"\n  Error during recognition: {e}")
                return None

    return None
