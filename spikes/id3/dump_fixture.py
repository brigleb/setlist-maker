"""Pull the chapters Python already embedded in a set's MP3 out as spike 4's input (run once).

Writes chapters.json, episode.json and the JPEGs to an output directory, so Python's
`embed_chapters` and the Rust spike can each embed exactly the same thing into a clone.

    uv run python spikes/id3/dump_fixture.py <set.mp3> <out-dir>
"""

import json
import sys
from pathlib import Path

from mutagen.id3 import ID3


def main(mp3: str, out: str) -> None:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tags = ID3(mp3)
    by_id = {f.element_id: f for f in tags.getall("CHAP")}
    (toc,) = tags.getall("CTOC")
    chapters = []
    for i, eid in enumerate(toc.child_element_ids):
        chap = by_id[eid]
        image = None
        pics = [f for f in chap.sub_frames.values() if f.FrameID == "APIC"]
        if pics:
            image = f"chp{i:03d}.jpg"
            (out_dir / image).write_bytes(pics[0].data)
        chapters.append(
            {
                "start_ms": chap.start_time,
                "end_ms": chap.end_time,
                "title": str(chap.sub_frames["TIT2"]),
                "image": image,
            }
        )
    (cover,) = tags.getall("APIC")
    (out_dir / "cover.jpg").write_bytes(cover.data)
    episode = {
        "title": str(tags["TIT2"]),
        "artist": str(tags["TPE1"]),
        "cover": "cover.jpg",
    }
    (out_dir / "chapters.json").write_text(json.dumps(chapters, indent=1, ensure_ascii=False))
    (out_dir / "episode.json").write_text(json.dumps(episode, indent=1, ensure_ascii=False))
    sizes = sorted((out_dir / c["image"]).stat().st_size for c in chapters if c["image"])
    print(
        f"{len(chapters)} chapters, {len(sizes)} with art ({sizes[0]}-{sizes[-1]} bytes),"
        f" cover {len(cover.data)} bytes"
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
