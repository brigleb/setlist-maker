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

from mutagen.id3 import APIC, CHAP, CTOC, TALB, TIT2, TPE1, CTOCFlags, Encoding, PictureType
from mutagen.mp3 import MP3

from setlist_maker.artwork import CoverImageError, create_chapter_image, load_cover_image
from setlist_maker.artwork_cache import chapter_image, source_artwork
from setlist_maker.episode import episode_tags
from setlist_maker.tracklist import Track, Tracklist
from setlist_maker.uploads import artwork_dir_for, cover_path_for


def embed_chapters(
    audio_path: Path,
    tracks: list[Track],
    chapter_images: dict[int, bytes] | None = None,
    episode_image: bytes | None = None,
    audio_duration_ms: int | None = None,
    title: str | None = None,
    artist: str | None = None,
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
        title: The episode title, written as both title (TIT2) and album
            (TALB). Blank or None leaves whatever the file already has.
        artist: The episode artist (TPE1), likewise.

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

    # Episode-level text. setall replaces only the top-level frame; a chapter's
    # TIT2 is a sub-frame of its CHAP and is not touched. A blank value leaves
    # the file's own tag, since the recorder may already have written one.
    if title and title.strip():
        audio.tags.setall("TIT2", [TIT2(encoding=Encoding.UTF8, text=[title.strip()])])
        audio.tags.setall("TALB", [TALB(encoding=Encoding.UTF8, text=[title.strip()])])
    if artist and artist.strip():
        audio.tags.setall("TPE1", [TPE1(encoding=Encoding.UTF8, text=[artist.strip()])])

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

        # Build chapter title. Joined from whichever halves the track has, so a
        # row that knows only its title -- which the markdown can now carry
        # across a save (#44), making this reachable in practice -- gets a
        # chapter called "Titled Only" rather than " - Titled Only".
        chapter_title = " - ".join(
            part for part in (track.artist.strip(), track.title.strip()) if part
        )
        if not chapter_title:
            chapter_title = "Unknown Track"

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


def _episode_cover_image(
    tracklist: Tracklist, chapter_tracks: list[Track], uploads_dir: Path | None = None
) -> bytes | None:
    """Build the episode-level cover from one track's artwork, relabelled for the set.

    Prefers the track the user starred in the editor; otherwise falls back to
    the original rule, the first track with *real* artwork. The starred track
    is only a preference, not a guarantee -- if its lookup turns up nothing the
    search continues down the list, because a set with a usable cover somewhere
    should get one rather than none.

    ``source_artwork()`` returns the same fetched image that track's chapter
    composite already used (a cache hit, no network) or None when nothing was
    findable, so one call answers both "is there art here" and "which art".
    None must keep meaning *skip*: handing it to ``create_chapter_image`` would
    produce a gradient card and, being truthy bytes, permanently block every
    later track from supplying a real cover.
    """
    identified = [t for t in chapter_tracks if not t.is_unidentified]
    starred = next((t for t in identified if t.is_episode_cover), None)
    if starred is not None:
        identified = [starred] + [t for t in identified if t is not starred]

    for track in identified:
        src = source_artwork(track.artist, track.title, track.coverart_url, uploads_dir=uploads_dir)
        if src:
            return create_chapter_image(
                artwork_bytes=src,
                artist=tracklist.source_file.replace("_tracklist", "").rsplit(".", 1)[0],
                title="Tracklist",
            )
    return None


def embed_chapters_for_tracklist(
    tracklist: Tracklist,
    audio_path: Path,
    fetch_art: bool = True,
    cover_image: bytes | None = None,
    tracklist_path: Path | None = None,
) -> tuple[int, int, bool]:
    """
    Embed chapter markers (and, optionally, artwork) into an MP3 for a tracklist.

    Shared by the `chapters` command and the `identify --chapters` chain.
    Assumes audio_path is a validated, existing MP3 and the tracklist has at
    least one identified track.

    ``cover_image`` (from --cover) becomes the episode cover verbatim, replacing
    the one normally derived from the first track's art. It is independent of
    ``fetch_art``, so a hand-picked cover can be embedded with or without
    per-track chapter images.

    ``tracklist_path`` locates what the web editor stores beside the tracklist
    (``uploads.py``): uploaded track artwork, and an uploaded episode cover,
    which ranks below ``--cover`` and above the starred track -- and, like
    ``--cover``, is embedded whether or not ``fetch_art`` is on. Every caller
    must pass it: without it an uploaded image is simply not found, and the
    chapter gets the gradient card with nothing said.

    It also supplies the episode's title and artist (``episode.py``): what the
    editor saved for the set, else the defaults.

    Returns ``(chapters, chapter images, whether an episode cover was embedded)``.
    """
    # Get all non-rejected tracks (including unidentified) for chapter timing
    chapter_tracks = [t for t in tracklist.tracks if not t.rejected]

    # Fetch artwork and generate chapter images
    chapter_images: dict[int, bytes] = {}
    # Seeding with the supplied cover both uses it and short-circuits the
    # first-track derivation below, which is already guarded on "still None"
    episode_image: bytes | None = cover_image
    uploads_dir = artwork_dir_for(tracklist_path) if tracklist_path else None
    if episode_image is None and tracklist_path is not None:
        saved_cover = cover_path_for(tracklist_path)
        if saved_cover.exists():
            try:
                episode_image = load_cover_image(saved_cover)
                print(f"  Cover image: {saved_cover.name}")
            except CoverImageError as e:
                print(f"  Warning: ignoring the saved episode cover: {e}")

    if fetch_art:
        print(f"\n{'─' * 60}")
        print("Fetching artwork...")

        for i, track in enumerate(chapter_tracks):
            if track.is_unidentified:
                print(f"  [{i + 1}/{len(chapter_tracks)}] {track.time_str} - Skipping unidentified")
                continue

            label = f"{track.artist} - {track.title}"
            print(f"  [{i + 1}/{len(chapter_tracks)}] {track.time_str} - {label}")

            # One cached path shared with the editor's preview, so the image
            # embedded here is byte-identical to the one the user approved.
            chapter_images[i] = chapter_image(
                artist=track.artist,
                title=track.title,
                coverart_url=track.coverart_url,
                uploads_dir=uploads_dir,
            )

        print(f"  Generated {len(chapter_images)} chapter image(s)")

        if episode_image is None:
            episode_image = _episode_cover_image(tracklist, chapter_tracks, uploads_dir)

    tags = episode_tags(tracklist_path) if tracklist_path else {"title": "", "artist": ""}

    # Embed chapters into MP3
    print(f"\n{'─' * 60}")
    print("Embedding chapter markers...")
    if tags["title"] or tags["artist"]:
        print(f"  Episode: {' - '.join(v for v in (tags['artist'], tags['title']) if v)}")

    embed_chapters(
        audio_path=audio_path,
        tracks=chapter_tracks,
        chapter_images=chapter_images if fetch_art else None,
        # Not gated on fetch_art: --cover --no-artwork embeds just the cover
        episode_image=episode_image,
        title=tags["title"],
        artist=tags["artist"],
    )

    print(f"\n  Embedded {len(chapter_tracks)} chapter(s) into {audio_path.name}")
    if chapter_images:
        print(f"  Embedded {len(chapter_images)} chapter image(s)")
    if episode_image:
        print("  Embedded episode cover art")

    print(f"\n{'=' * 60}")
    print("Done! Chapter markers embedded successfully.")
    print(f"{'=' * 60}")
    return len(chapter_tracks), len(chapter_images), episode_image is not None
