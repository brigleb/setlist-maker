"""
MP3 chapter marker embedding using ID3v2 CHAP/CTOC frames.

Embeds chapter markers into MP3 files for podcast player navigation,
including per-chapter artwork (APIC sub-frames) and an episode-level
cover image.

Uses the mutagen library to write ID3v2 tags following the
ID3v2 Chapter Frame Addendum v1.0 specification.
"""

import struct
from pathlib import Path

from mutagen.id3 import APIC, CHAP, CTOC, TIT2, CTOCFlags, Encoding, PictureType
from mutagen.mp3 import MP3

from setlist_maker.editor import Track


def embed_chapters(
    audio_path: Path,
    tracks: list[Track],
    chapter_images: dict[int, bytes] | None = None,
    episode_image: bytes | None = None,
    audio_duration_ms: int | None = None,
) -> Path:
    """
    Embed chapter markers and artwork into an MP3 file.

    Creates CHAP frames for each track with TIT2 (title) sub-frames
    and optional APIC (artwork) sub-frames. Wraps them in a CTOC
    frame for podcast player navigation.

    Args:
        audio_path: Path to the MP3 file to modify (in-place).
        tracks: List of tracks (non-rejected, in order).
        chapter_images: Optional mapping of track index -> JPEG bytes
            for per-chapter artwork.
        episode_image: Optional JPEG bytes for the episode-level cover.
        audio_duration_ms: Total audio duration in milliseconds. If not
            provided, it is read from the file.

    Returns:
        The audio_path (for convenience).

    Raises:
        FileNotFoundError: If the audio file doesn't exist.
        ValueError: If no tracks are provided.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if not tracks:
        raise ValueError("No tracks to embed as chapters")

    audio = MP3(str(audio_path))

    # Ensure ID3 tags exist
    if audio.tags is None:
        audio.add_tags()

    # Get audio duration
    if audio_duration_ms is None:
        audio_duration_ms = int(audio.info.length * 1000)

    # Remove any existing chapter-related frames
    _remove_existing_chapters(audio)

    # Add episode-level artwork before chapters so delall("APIC") doesn't
    # risk interfering with CHAP sub-frames
    if episode_image:
        audio.tags.delall("APIC")
        audio.tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=PictureType.COVER_FRONT,
                desc="Episode Cover",
                data=episode_image,
            )
        )

    # Build chapter element IDs
    chapter_ids = [f"chp{i:03d}" for i in range(len(tracks))]

    # Add CHAP frames for each track
    for i, track in enumerate(tracks):
        start_ms = track.timestamp * 1000

        # End time is start of next track, or audio end for last track
        if i + 1 < len(tracks):
            end_ms = tracks[i + 1].timestamp * 1000
        else:
            end_ms = audio_duration_ms

        # Build chapter title
        if track.is_unidentified:
            chapter_title = "Unknown Track"
        else:
            chapter_title = f"{track.artist} - {track.title}"

        # Build sub-frames
        sub_frames = [
            TIT2(encoding=Encoding.UTF8, text=[chapter_title]),
        ]

        # Add per-chapter artwork if available
        if chapter_images and i in chapter_images:
            sub_frames.append(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=PictureType.COVER_FRONT,
                    desc=f"Chapter {i + 1}",
                    data=chapter_images[i],
                )
            )

        audio.tags.add(
            CHAP(
                element_id=chapter_ids[i],
                start_time=start_ms,
                end_time=end_ms,
                sub_frames=sub_frames,
            )
        )

    # Add CTOC (Table of Contents) frame
    audio.tags.add(
        CTOC(
            element_id="toc",
            flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
            child_element_ids=chapter_ids,
            sub_frames=[
                TIT2(encoding=Encoding.UTF8, text=["Table of Contents"]),
            ],
        )
    )

    # Save as ID3v2.3, not mutagen's default v2.4: players (Apple Podcasts,
    # ffmpeg) misparse v2.4 syncsafe CHAP sub-frame sizes once artwork pushes
    # a sub-frame past 128 bytes, discarding all chapters (issue #17).
    audio.save(v2_version=3)
    _order_chap_frames_chronologically(audio_path)
    return audio_path


def _order_chap_frames_chronologically(audio_path: Path) -> None:
    """Rewrite the just-saved ID3v2.3 tag so its CHAP frames sit in time order.

    mutagen sorts every frame at save time by serialized size (only APIC keeps
    insertion order), so per-chapter artwork of differing sizes scatters CHAP
    frames through the tag. CTOC still names the presentation order and
    conforming players follow it, but ffmpeg/ffprobe -- and every host or web
    player built on them -- enumerate CHAP frames in file order and show a
    shuffled chapter list (issue #33). mutagen exposes no ordering hook and all
    of its serialization is private, so this permutes the frames in the bytes it
    wrote instead. The tag is a fixed-size region (header, frames, padding), so
    reordering frames inside it never moves the audio; anything this parser does
    not expect (another version, tag or frame flags, a short frame) leaves the
    file exactly as mutagen saved it, which is the pre-fix status quo.
    """
    with open(audio_path, "r+b") as f:
        header = f.read(10)
        # Only the shape save(v2_version=3) writes: v2.3, no unsync/extended
        # header flags. Tag size is a 4-byte syncsafe integer (7 bits per byte).
        if header[:6] != b"ID3\x03\x00\x00":
            return
        tag_size = 0
        for byte in header[6:10]:
            tag_size = (tag_size << 7) | (byte & 0x7F)
        body = f.read(tag_size)

        frames: list[tuple[bytes, bytes]] = []  # (frame id, raw frame incl. header)
        try:
            pos = 0
            while pos + 10 <= len(body):
                frame_id = body[pos : pos + 4]
                if frame_id == b"\x00\x00\x00\x00":
                    break  # padding
                # v2.3 frame sizes are plain big-endian, not syncsafe
                (size,) = struct.unpack(">L", body[pos + 4 : pos + 8])
                end = pos + 10 + size
                if end > len(body):
                    raise ValueError("truncated frame")
                frames.append((frame_id, body[pos:end]))
                pos = end
            chaps = [raw for frame_id, raw in frames if frame_id == b"CHAP"]
            ordered = sorted(chaps, key=_chap_start_ms)
        except (ValueError, struct.error):
            return  # something this parser does not understand: leave the tag as saved
        if ordered == chaps:
            return

        by_time = iter(ordered)
        f.seek(10)
        f.write(b"".join(next(by_time) if fid == b"CHAP" else raw for fid, raw in frames))


def _chap_start_ms(raw: bytes) -> int:
    """Start time of a raw v2.3 CHAP frame: NUL-terminated element id, then uint32 ms."""
    if raw[8:10] != b"\x00\x00":
        raise ValueError("frame flags would shift the CHAP payload")  # mutagen sets none
    at = raw.index(b"\x00", 10) + 1
    return struct.unpack(">L", raw[at : at + 4])[0]


def _remove_existing_chapters(audio: MP3) -> None:
    """Remove any existing CHAP and CTOC frames from the file."""
    if audio.tags is None:
        return

    # Collect keys to delete (can't modify dict during iteration)
    to_delete = [key for key in audio.tags if key.startswith(("CHAP:", "CTOC:"))]
    for key in to_delete:
        del audio.tags[key]
