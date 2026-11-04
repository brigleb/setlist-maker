"""Describe an MP3's ID3v2.3 tag frame by frame, in file order, as JSON (spike 4's diff).

A walk of the raw bytes, separate from both writers. Images are reduced to a length and a
hash, and the padding to its length. mutagen then has to load the tag too.

    uv run python spikes/id3/describe.py <file.mp3>
"""

import hashlib
import json
import struct
import sys

from mutagen.id3 import ID3


def text(enc: int, b: bytes) -> str:
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}[enc]
    return b.decode(codec).rstrip("\x00")


def cstr(b: bytes) -> tuple[str, bytes]:
    i = b.index(b"\x00")
    return b[:i].decode("latin-1"), b[i + 1 :]


def frame(fid: str, flags: int, data: bytes) -> dict:
    d: dict = {"id": fid, "flags": flags, "size": len(data)}
    if fid.startswith("T") and fid != "TXXX":
        d |= {"enc": data[0], "text": text(data[0], data[1:])}
    elif fid == "APIC":
        mime, rest = cstr(data[1:])
        ptype, rest = rest[0], rest[1:]
        term = b"\x00\x00" if data[0] in (1, 2) else b"\x00"
        desc_end = next(
            i for i in range(0, len(rest), len(term)) if rest[i : i + len(term)] == term
        )
        d |= {
            "enc": data[0],
            "mime": mime,
            "type": ptype,
            "desc": text(data[0], rest[:desc_end]),
            "image": [
                len(rest) - desc_end - len(term),
                hashlib.sha256(rest[desc_end + len(term) :]).hexdigest()[:12],
            ],
        }
    elif fid == "CHAP":
        eid, rest = cstr(data)
        start, end, so, eo = struct.unpack(">4L", rest[:16])
        d |= {
            "element": eid,
            "start": start,
            "end": end,
            "offsets": [so, eo],
            "sub": walk(rest[16:])[0],
        }
    elif fid == "CTOC":
        eid, rest = cstr(data)
        flags, count, rest = rest[0], rest[1], rest[2:]
        kids = []
        for _ in range(count):
            k, rest = cstr(rest)
            kids.append(k)
        d |= {"element": eid, "toc_flags": flags, "children": kids, "sub": walk(rest)[0]}
    else:
        d["sha"] = hashlib.sha256(data).hexdigest()[:12]
    return d


def walk(body: bytes) -> tuple[list[dict], int]:
    out, pos = [], 0
    while pos + 10 <= len(body) and body[pos] != 0:
        fid = body[pos : pos + 4].decode("latin-1")
        size, flags = struct.unpack(">LH", body[pos + 4 : pos + 10])
        out.append(frame(fid, flags, body[pos + 10 : pos + 10 + size]))
        pos += 10 + size
    return out, len(body) - pos


def main(path: str) -> None:
    with open(path, "rb") as f:
        head = f.read(10)
        size = 0
        for b in head[6:10]:
            size = (size << 7) | (b & 0x7F)
        frames, padding = walk(f.read(size))
        f.seek(10 + size)
        audio = hashlib.sha256(f.read()).hexdigest()[:16]
    tags = ID3(path)
    print(
        json.dumps(
            {
                "header": head[:6].hex(),
                "tag_bytes": 10 + size,
                "padding": padding,
                "audio_sha": audio,
                "mutagen": {
                    "version": list(tags.version),
                    "chap": len(tags.getall("CHAP")),
                    "ctoc": len(tags.getall("CTOC")),
                },
                "frames": frames,
            },
            indent=1,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main(sys.argv[1])
