"""Embed spike 4's fixture with Python's own `embed_chapters`, the reference (A/B side A).

uv run python spikes/id3/python_embed.py <fixture-dir> <clone.mp3>
"""

import json
import sys
import time
from pathlib import Path

from setlist_maker.chapters import embed_chapters
from setlist_maker.tracklist import Track


def main(fixture: str, mp3: str) -> None:
    d = Path(fixture)
    chapters = json.loads((d / "chapters.json").read_text())
    episode = json.loads((d / "episode.json").read_text())
    assert all(c["start_ms"] % 1000 == 0 for c in chapters), "Track starts are whole seconds"
    # The chapter title is "artist - title" joined from what's there, so the whole title as
    # the artist reproduces it exactly.
    tracks = [Track(timestamp=c["start_ms"] // 1000, artist=c["title"], title="") for c in chapters]
    images = {i: (d / c["image"]).read_bytes() for i, c in enumerate(chapters) if c["image"]}
    t0 = time.perf_counter()
    embed_chapters(
        Path(mp3),
        tracks,
        chapter_images=images,
        episode_image=(d / episode["cover"]).read_bytes(),
        audio_duration_ms=chapters[-1]["end_ms"],
        title=episode["title"],
        artist=episode["artist"],
    )
    print(f"python embed_chapters: {(time.perf_counter() - t0) * 1000:.0f}ms")


if __name__ == "__main__":
    main(*sys.argv[1:])
