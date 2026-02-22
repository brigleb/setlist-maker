"""
MP3 chapter marker embedding using ID3v2 CHAP/CTOC frames.

Embeds chapter markers into MP3 files for podcast player navigation,
including per-chapter artwork (APIC sub-frames) and an episode-level
cover image.

Uses the mutagen library to write ID3v2 tags following the
ID3v2 Chapter Frame Addendum v1.0 specification.
"""

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

    # Add episode-level artwork
    if episode_image:
        # Remove existing cover art first
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

    audio.save()
    return audio_path


def _remove_existing_chapters(audio: MP3) -> None:
    """Remove any existing CHAP and CTOC frames from the file."""
    if audio.tags is None:
        return

    # Collect keys to delete (can't modify dict during iteration)
    to_delete = [key for key in audio.tags if key.startswith(("CHAP:", "CTOC:"))]
    for key in to_delete:
        del audio.tags[key]
