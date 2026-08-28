"""Shazam recognition with exponential-backoff retry for rate limits."""

import asyncio
import random
from collections.abc import Callable
from pathlib import Path

from pydub import AudioSegment
from shazamio import Shazam

MAX_RETRIES = 5
INITIAL_BACKOFF = 30


def estimate_confidence(result: dict) -> float:
    """
    Heuristic match-confidence score in [0, 1] for a Shazam result.

    Shazam does not expose a single confidence number, so this combines two
    weak signals: how well each fingerprint match aligns (low frequency skew
    is better) and how many matches corroborate the same track. It is a
    placeholder proxy -- good enough to rank matches and to tell a strong
    one-off hit from a stray false positive -- not a calibrated probability.
    """
    matches = result.get("matches") or []
    if not matches:
        # A track was returned but with no match detail; stay neutral.
        return 0.5

    alignments = []
    for m in matches:
        freq_skew = abs(m.get("frequencyskew", 0) or 0)
        alignments.append(max(0.0, 1.0 - freq_skew))
    alignment = sum(alignments) / len(alignments)
    corroboration = min(1.0, len(matches) / 3)
    return round(0.5 * alignment + 0.5 * corroboration, 3)


async def identify_sample_with_retry(
    shazam: Shazam,
    segment: AudioSegment,
    temp_dir: str,
    max_retries: int = MAX_RETRIES,
    on_backoff: Callable[[float, int], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> dict | None:
    """
    Identify a single audio segment using Shazam with exponential backoff retry.
    Returns track info dict or None if not identified.

    `on_backoff(wait_seconds, attempt)` is called just before each rate-limit
    sleep. It exists so the live progress panel can count the backoff down
    rather than leaving 30 seconds of silence; when it is given, the caller owns
    announcing the wait and the warning is not printed here as well.

    `on_error(exc)` is called with any exception that ends the attempt. Every
    failure here collapses to a `None` return, which the pipeline cannot tell
    apart from audio Shazam genuinely does not know -- notably a rate limit,
    which arrives as `FailedDecodeJson("Failed to decode json")` and carries its
    429 only on the chained cause. The hook exists so the call log can record
    the exception *type* before that distinction is thrown away.
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
                    "confidence": estimate_confidence(result),
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
                    if on_backoff is not None:
                        on_backoff(wait_time, attempt + 1)
                    else:
                        print(
                            f"\n  Warning: Rate limited. Backing off for {wait_time:.0f} seconds "
                            f"(attempt {attempt + 1}/{max_retries})..."
                        )
                    await asyncio.sleep(wait_time)
                    backoff *= 2  # Exponential backoff
                else:
                    if on_error is not None:
                        on_error(e)
                    print(f"\n  Error: Rate limit persisted after {max_retries} attempts")
                    return None
            else:
                # Other error - log and return None
                if on_error is not None:
                    on_error(e)
                print(f"\n  Error during recognition: {e}")
                return None

    return None
