"""
The tracklist model and its persistence: what a set contains and how it is saved.

``Track`` and ``Tracklist``, the markdown format (``to_markdown()`` and
``parse_markdown_tracklist()``), the JSON sidecar joined onto it
(``load_tracklist()``), the corrections learned from edits (``CorrectionsDB``,
``apply_track_edit()``) and ``save_tracklist()``. Every front end -- the identify
run, the TUI, the web editor, ``chapters`` -- goes through this module, which is
why it imports nothing heavier than the standard library.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from setlist_maker import AUDIO_EXTENSIONS
from setlist_maker.fileio import atomic_write_text, config_dir

logger = logging.getLogger(__name__)

# The set description is free text the user types, so it can contain any shape
# this format otherwise uses -- including a line that reads exactly like a
# track. Fencing it makes the file unambiguous instead of merely unlikely (#16).
# HTML comments because the .md is a published artifact (show notes) and these
# leave the rendered page untouched: checked against markdown-it, python-markdown,
# mistune and commonmark, all of which end the HTML block on the line carrying
# `-->` (CommonMark block type 2, so cmark-gfm and GitHub agree) and render the
# description as its own paragraph, exactly as before.
SUMMARY_OPEN_MARKER = "<!-- summary -->"
SUMMARY_CLOSE_MARKER = "<!-- /summary -->"

# Either marker, with any number of backslashes already in front of it. Built
# from the markers themselves: spelled out again, a renamed marker would stop
# being escaped and the fence would quietly become breakable.
_SUMMARY_MARKER_LOOKALIKE = re.compile(
    rf"^\\*(?:{re.escape(SUMMARY_OPEN_MARKER)}|{re.escape(SUMMARY_CLOSE_MARKER)})$"
)

# Stand-ins for a field the listing has no value for. Italic, and that is what
# makes them unmistakable rather than merely unlikely: a real artist is always
# written **bold**, so no value can serialize to either of these, however it is
# spelled -- an artist genuinely named "*Unknown artist*" comes out as
# ***Unknown artist*** and reads back as itself. Leaving the empty side out
# entirely would have been prettier and ambiguous ("1. Titled Only (3:00)" is
# indistinguishable from prose), which is the trade #16 already settled.
UNIDENTIFIED_MARKER = "*Unidentified*"
UNKNOWN_ARTIST_MARKER = "*Unknown artist*"

# One listing line. Four shapes, because a track may know its artist, its title,
# both, or neither (#44) -- and the file is the authority on which tracks exist,
# so a shape missing here is a row silently deleted on reopen, not a row that
# merely looks wrong. Ordered marker-branches first; they cannot collide with
# the bold ones, which start with two asterisks.
TRACK_LINE_PATTERN = re.compile(
    r"^\d+\.\s+"
    r"(?:"
    rf"{re.escape(UNKNOWN_ARTIST_MARKER)}\s*-\s*(?P<titled>.+?)"  # *Unknown artist* - Title
    r"|"
    rf"{re.escape(UNIDENTIFIED_MARKER)}"  # *Unidentified*
    r"|"
    # The artist span accepts nothing between the markers so that files already
    # written as "**** - Title" by the old writer give their track back instead
    # of staying short. The *title* span must stay `.+?`: the pattern is
    # unanchored, so a title allowed to match nothing lets the time group bind
    # to a parenthesized time inside the title -- "**A** - (1:23) Reprise
    # (10:00)" would read as title "" at 0:01:23, losing the title and moving
    # the row, which then misses its sidecar entry (joined by timestamp) and
    # its chapter mark. The legacy "**Artist** -  " shape needs no help here
    # anyway: `\s*-\s*` is greedy, the span takes the space it leaves, and the
    # caller strips it.
    r"\*\*(?P<artist>.*?)\*\*\s*-\s*(?P<title>.+?)"  # **Artist** - Title
    r"|"
    r"\*\*(?P<artist_only>.+?)\*\*"  # **Artist**
    r")\s*"
    r"\((?P<time>\d+:\d+(?::\d+)?)\)"  # (MM:SS) or (H:MM:SS)
)


def _escape_summary_line(line: str) -> str:
    """Neutralize a description line that would otherwise read as a marker.

    One leading backslash, which markdown renders away, so the line still reads
    as itself; ``_unescape_summary_line`` removes exactly one. Prefixing and
    stripping one is an involution, so a description that genuinely contains
    ``\\<!-- /summary -->`` survives the round trip too. The backslash is spliced
    into the line rather than replacing it, so the user's own indentation --
    which decides nothing here, since the fence is matched on stripped lines --
    comes back untouched as well.
    """
    stripped = line.strip()
    if not _SUMMARY_MARKER_LOOKALIKE.match(stripped):
        return line
    return line.replace(stripped, f"\\{stripped}", 1)


def _unescape_summary_line(line: str) -> str:
    """Undo :func:`_escape_summary_line` for one line of a fenced description."""
    stripped = line.strip()
    if stripped.startswith("\\") and _SUMMARY_MARKER_LOOKALIKE.match(stripped):
        return line.replace(stripped, stripped[1:], 1)
    return line


@dataclass
class Track:
    """Represents a single track in the tracklist."""

    timestamp: int  # seconds from start
    artist: str
    title: str
    rejected: bool = False
    shazam_url: str | None = None
    album: str | None = None
    coverart_url: str | None = None
    # True once the user picked this cover in the editor, which turns
    # coverart_url from "what Shazam said" into "what the user chose" -- the
    # difference apply_track_edit() needs to know about (#20).
    artwork_pinned: bool = False
    # Whose artwork becomes the episode-level cover; at most one track carries it.
    is_episode_cover: bool = False
    confidence: float | None = None  # Shazam match-confidence proxy, if known
    original_artist: str | None = None  # For tracking corrections
    original_title: str | None = None

    @property
    def time_str(self) -> str:
        """Format timestamp as HH:MM:SS or MM:SS."""
        hours = self.timestamp // 3600
        minutes = (self.timestamp % 3600) // 60
        secs = self.timestamp % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    @property
    def is_unidentified(self) -> bool:
        """Check if this track was not identified by Shazam.

        Whitespace-tolerant: a field holding only spaces names nothing, and
        reads back as empty anyway, so calling it identified only meant the
        difference surfaced later -- as a chapter titled "   -   " (#44).
        """
        return not self.artist.strip() and not self.title.strip()

    @property
    def was_corrected(self) -> bool:
        """Check if this track was manually corrected."""
        if self.original_artist is None and self.original_title is None:
            return False
        return self.artist != self.original_artist or self.title != self.original_title


@dataclass
class Tracklist:
    """A complete tracklist for an audio file."""

    source_file: str
    tracks: list[Track] = field(default_factory=list)
    generated_on: str | None = None
    summary: str | None = None

    def to_markdown(self) -> str:
        """Generate markdown output from the tracklist."""
        lines = [
            f"# Tracklist: {self.source_file}",
            "",
            f"*Generated on {self.generated_on or datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            "",
        ]

        if self.summary and self.summary.strip():
            # Fenced so reopening cannot mistake the description for the
            # listing, and vice versa (#16). The markers render as nothing.
            #
            # Carriage returns are normalized here rather than at the far end
            # because this is the side that decides where the lines are: the
            # reader opens the file in text mode, where a lone CR is a line
            # break, so a description carrying one would be split at a point
            # this never escaped -- and a "<!-- /summary -->" after it would
            # close the fence from the inside.
            #
            # The blank lines inside the fence are for renderers that escape
            # raw HTML instead of honoring it (some podcast hosts): they cost
            # nothing where comments are invisible, and where they are not,
            # they keep the description a paragraph of its own instead of one
            # run-on with the markers.
            summary = self.summary.replace("\r\n", "\n").replace("\r", "\n").strip()
            lines.append(SUMMARY_OPEN_MARKER)
            lines.append("")
            lines.extend(_escape_summary_line(ln) for ln in summary.split("\n"))
            lines.append("")
            lines.append(SUMMARY_CLOSE_MARKER)
            lines.append("")

        track_num = 1
        for track in self.tracks:
            if track.rejected:
                continue
            time_str = track.time_str
            # Written from the stripped values so a field of only spaces is
            # stored as the empty field it already is, rather than as one that
            # becomes empty on the next read -- which is how the whitespace
            # case used to survive one round trip and die on the following one.
            artist = track.artist.strip()
            title = track.title.strip()
            if track.is_unidentified:
                lines.append(f"{track_num}. {UNIDENTIFIED_MARKER} ({time_str})")
            elif not artist:
                lines.append(f"{track_num}. {UNKNOWN_ARTIST_MARKER} - {title} ({time_str})")
            elif not title:
                lines.append(f"{track_num}. **{artist}** ({time_str})")
            else:
                lines.append(f"{track_num}. **{artist}** - {title} ({time_str})")
            track_num += 1

        lines.append("")
        return "\n".join(lines)

    def to_json(self) -> list[dict]:
        """Export tracklist as JSON-serializable list.

        Stays a bare list even though the episode-cover choice is conceptually
        a property of the *set*: it is expressed as a flag on the one track
        whose art is used, so the sidecar's top-level shape never changes.
        Wrapping it in an object would be read as a dict by every existing
        reader -- ``load_tracklist`` iterates it, and
        iterating a dict yields harmless-looking strings rather than raising,
        so the failure is silent: every track loses its ``coverart_url`` and
        re-keys the artwork cache.
        """
        return [
            {
                "timestamp": t.timestamp,
                "time": t.time_str,
                "artist": t.artist,
                "title": t.title,
                "rejected": t.rejected,
                "shazam_url": t.shazam_url,
                "album": t.album,
                "coverart_url": t.coverart_url,
                "artwork_pinned": t.artwork_pinned,
                "episode_cover": t.is_episode_cover,
                "confidence": t.confidence,
            }
            for t in self.tracks
            if not t.rejected
        ]


def _parse_summary(lines: list[str]) -> tuple[str | None, set[int]]:
    """Recover the set description, and which lines it occupies.

    Two forms, because files written before the fence existed have to keep
    reopening. Fenced (what ``to_markdown`` now writes): everything between the
    markers verbatim, bar the whitespace around the block as a whole, which is
    stripped on both sides -- so blank lines and track-shaped lines *inside* a
    description are just text. Legacy (no markers): the old heuristic --
    contiguous prose after the "*Generated on*" line, ending at the first blank
    or track-shaped line. That form is structurally ambiguous and nothing can
    recover a description the format itself made indistinguishable from a
    track; the next save rewrites the file fenced.

    The returned line numbers are what keeps the *other* half of #16 from
    happening: every later scan skips them, so a description can no longer
    contribute a phantom track, date or header.
    """
    start = next((i for i, ln in enumerate(lines) if ln.strip() == SUMMARY_OPEN_MARKER), None)
    if start is not None:
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].strip() == SUMMARY_CLOSE_MARKER),
            None,
        )
        if end is not None:
            text = "\n".join(_unescape_summary_line(ln) for ln in lines[start + 1 : end]).strip()
            return text or None, set(range(start, end + 1))
        # An opening marker with nothing closing it is a damaged file, not an
        # old one, and the two are distinguishable -- so say so. Degrading
        # silently here would reintroduce, one case over, the quiet loss this
        # fence exists to end.
        print(f"  Warning: '{SUMMARY_OPEN_MARKER}' has no closing marker; reading the")
        print("  set description as unfenced prose. Some of it may be missing.")

    # No fence, or an opening marker a hand edit left unclosed: fall back to the
    # legacy scan rather than swallow the rest of the file as a description.
    summary_lines: list[str] = []
    span: set[int] = set()
    seen_generated = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("*Generated on"):
            seen_generated = True
            continue
        if not seen_generated:
            continue
        if stripped in (SUMMARY_OPEN_MARKER, SUMMARY_CLOSE_MARKER):
            span.add(i)
            continue
        if TRACK_LINE_PATTERN.match(stripped):
            break
        if stripped:
            summary_lines.append(stripped)
            span.add(i)
        elif summary_lines:
            break
    return (" ".join(summary_lines) or None), span


def parse_markdown_tracklist(content: str) -> Tracklist:
    """Parse a markdown tracklist file into a Tracklist object."""
    # Line endings are normalized once, up front, because the fenced description
    # is now kept *verbatim*: every other field is matched with .strip(), which
    # absorbs a stray CR, but a multi-line description would carry one into the
    # middle of the text and back out to the editor.
    lines = content.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")
    tracklist = Tracklist(source_file="")

    # The description comes first, and everything below reads around it: it is
    # user-typed prose that may contain a header, a date or a track line, none
    # of which are this file's own (#16).
    tracklist.summary, summary_span = _parse_summary(lines)
    body = [line for i, line in enumerate(lines) if i not in summary_span]

    # Parse header: # Tracklist: filename.mp3
    for line in body:
        if line.startswith("# Tracklist:"):
            tracklist.source_file = line.replace("# Tracklist:", "").strip()
            break

    # Parse generation date: *Generated on YYYY-MM-DD HH:MM*
    for line in body:
        if line.startswith("*Generated on"):
            match = re.search(r"\*Generated on (.+)\*", line)
            if match:
                tracklist.generated_on = match.group(1)
            break

    # Parse tracks: "1. **Artist** - Title (MM:SS)" or "1. *Unidentified* (MM:SS)"
    for line in body:
        match = TRACK_LINE_PATTERN.match(line.strip())
        if match:
            # Each shape fills a different pair of groups and leaves the rest
            # None; an empty string from the shape that *did* match is the same
            # answer as the fallback, so the chain never picks up a wrong one.
            artist = match.group("artist") or match.group("artist_only") or ""
            title = match.group("title") or match.group("titled") or ""
            time_str = match.group("time")

            # Parse timestamp
            parts = time_str.split(":")
            if len(parts) == 3:
                timestamp = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                timestamp = int(parts[0]) * 60 + int(parts[1])

            track = Track(
                timestamp=timestamp,
                artist=artist.strip(),
                title=title.strip(),
                original_artist=artist.strip() if artist else None,
                original_title=title.strip() if title else None,
            )
            tracklist.tracks.append(track)

    # A closing marker moved to the far side of the listing -- only a hand edit
    # puts one there -- swallows every track into the description, and the next
    # save writes that reading back as a perfectly well-formed file, taking the
    # sidecar's cover art with it. The parse stands (guessing which half is the
    # description would be its own silent failure), but it says so first.
    if not tracklist.tracks and any(
        TRACK_LINE_PATTERN.match(lines[i].strip()) for i in summary_span
    ):
        print(
            f"  Warning: the set description runs past the listing; check the "
            f"'{SUMMARY_CLOSE_MARKER}'"
        )
        print("  marker's position. No tracks were read from this file.")

    return tracklist


def find_audio_file(markdown_path: Path) -> Path | None:
    """
    Find the audio file matching a markdown tracklist.

    Looks for audio files with the same base name as the markdown file
    in the same directory.

    Args:
        markdown_path: Path to the markdown tracklist file

    Returns:
        Path to the matching audio file, or None if not found
    """
    base_name = markdown_path.stem
    parent_dir = markdown_path.parent

    # First, try exact base name match
    for ext in AUDIO_EXTENSIONS:
        candidate = parent_dir / f"{base_name}{ext}"
        if candidate.exists():
            return candidate

    # Also try removing _tracklist suffix if present
    if base_name.endswith("_tracklist"):
        audio_name = base_name[: -len("_tracklist")]
        for ext in AUDIO_EXTENSIONS:
            candidate = parent_dir / f"{audio_name}{ext}"
            if candidate.exists():
                return candidate

    return None


def resolve_audio_path(audio_path: Path | None, output_path: Path) -> Path | None:
    """Locate the source audio for previewing track segments.

    Prefers an explicitly-provided path (the fresh-identify case); falls back
    to discovering a sibling of the markdown file. Returns None if neither
    resolves, so a moved/renamed file degrades gracefully.
    """
    if audio_path is not None and audio_path.exists():
        return audio_path
    return find_audio_file(output_path)


def load_tracklist(tracklist_path: Path) -> Tracklist:
    """
    Load a tracklist from its markdown, joining on what the JSON sidecar adds.

    The markdown says which tracks exist; the sidecar carries what it cannot:
    each track's ``coverart_url``, ``artwork_pinned`` and episode-cover star.
    Every loader that feeds the editor or ``chapters`` must come through here --
    parsing the markdown alone leaves ``coverart_url`` None and silently keys
    the artwork cache differently from the embed.
    """
    # Try loading from JSON sidecar for richer metadata
    json_path = tracklist_path.with_suffix(".json")
    if json_path.exists():
        try:
            with open(json_path) as f:
                json_tracks = json.load(f)

            # Parse markdown for the canonical tracklist structure
            with open(tracklist_path) as f:
                tracklist = parse_markdown_tracklist(f.read())

            # Map coverart URLs from JSON to tracklist tracks by timestamp
            # (index mapping breaks when rejected tracks are excluded from JSON)
            json_by_timestamp = {jt["timestamp"]: jt for jt in json_tracks if "timestamp" in jt}
            for track in tracklist.tracks:
                jt = json_by_timestamp.get(track.timestamp)
                if jt:
                    url = jt.get("coverart_url")
                    # isinstance, not truthiness: a hand-edited sidecar holding
                    # a number here would reach cache_key's str join and
                    # resize_cover_art_url's re.sub, both outside this try.
                    if url and isinstance(url, str):
                        track.coverart_url = url
                        track.artwork_pinned = bool(jt.get("artwork_pinned"))
                    # Curated in the editor: whose art becomes the episode cover.
                    track.is_episode_cover = bool(jt.get("episode_cover"))

            return tracklist
        except (json.JSONDecodeError, IOError, TypeError):
            # TypeError covers a sidecar that is valid JSON but the wrong shape
            # (null, a number, a list of scalars): iterating it, or testing
            # membership in its elements, raises rather than failing to parse.
            # Any such sidecar degrades to markdown-only, as an unreadable one does.
            pass

    # Fallback: parse markdown only (no coverart URLs available)
    with open(tracklist_path) as f:
        return parse_markdown_tracklist(f.read())


# Reissue bookkeeping in a title: "(2009 Remaster)", "[Remastered]", "- 2012 Remaster".
# Mirrors cleanTitle() in web_timeline.js, whose "Tidy" fix removes exactly this.
_REISSUE_TAG = re.compile(
    r"\s*[(\[](?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:version|\d{4}))?[)\]]",
    re.IGNORECASE,
)
_REISSUE_DASH = re.compile(
    r"\s+-\s+(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:version|\d{4}))?\s*$",
    re.IGNORECASE,
)


def strip_reissue_tag(title: str) -> str:
    """A title without its remaster tag; "(Vocal)" and the like are left alone."""
    return _REISSUE_DASH.sub("", _REISSUE_TAG.sub("", title or "")).strip()


def apply_track_edit(
    track: Track,
    artist: str,
    title: str,
    corrections_db: "CorrectionsDB | None" = None,
) -> bool:
    """Apply an artist/title correction to one track, recording it for learning.

    Shared by the TUI editor and the web editor so both front ends learn
    corrections identically and cannot drift apart.

    Clears ``coverart_url`` unless the user pinned it. That URL is normally
    evidence attached to the *original* Shazam identification, and
    ``fetch_artwork()`` tries it ahead of every search. Left in place on a
    correction it short-circuits the waterfall, so the chapter image keeps
    showing the misidentified track's cover under the corrected text --
    regenerating, because artist and title are in the cache key, but
    regenerating the same wrong picture (#30). Dropping it lets iTunes /
    Deezer / MusicBrainz search on what the user actually said the track is.

    Tidying a remaster tag off a title is not a correction of *which* track it
    is, so it keeps the URL too.

    ``artwork_pinned`` is the other exception, and the reason the field exists: a
    cover chosen in the editor's picker is not Shazam's guess about the old
    identification, it is the user's answer about this track, and clearing it
    on a later typo fix would silently throw that answer away (#20).

    Both fields are stripped first, so surrounding space is never what a
    "change" consists of: the web editor already strips at its own door, and
    matching it here keeps stored fields uniformly bare, which is what lets the
    markdown decide a row's shape from the value alone (#44).

    Returns True if the track changed.
    """
    artist = artist.strip()
    title = title.strip()
    if artist == track.artist and title == track.title:
        return False

    # Remember what Shazam said, so was_corrected and the display can tell
    if track.original_artist is None:
        track.original_artist = track.artist
    if track.original_title is None:
        track.original_title = track.title

    # Removing a remaster tag names the same recording, so Shazam's cover is
    # still the right one; clearing it would send every tidied track back
    # through a search that can find another edition's art, or none.
    same_recording = artist == track.artist and strip_reissue_tag(title) == strip_reissue_tag(
        track.title
    )
    track.artist = artist
    track.title = title
    if not track.artwork_pinned and not same_recording:
        track.coverart_url = None

    if corrections_db and track.was_corrected:
        corrections_db.add_correction(
            original_artist=track.original_artist or "",
            original_title=track.original_title or "",
            corrected_artist=artist,
            corrected_title=title,
        )
    return True


def save_tracklist(
    tracklist: Tracklist,
    output_path: Path,
    corrections_db: "CorrectionsDB | None" = None,
) -> None:
    """Write a tracklist to markdown + its JSON sidecar, and persist corrections.

    Shared by the TUI editor, the web editor and the identify run's own output,
    so all three produce identical files and cannot drift apart.

    Each file is replaced atomically (#64): the ``.md`` is authoritative for
    which tracks exist and the description's only store, so a crash mid-write
    must leave the previous one, never a truncated one. The sidecar goes
    first. If only one of the two lands, new sidecar + old ``.md`` is harmless
    (it is joined by timestamp, and a row it lacks just has no artwork), while
    new ``.md`` + old sidecar would re-join a corrected track to the stale
    Shazam URL it was corrected away from (#30).
    """
    atomic_write_text(output_path.with_suffix(".json"), json.dumps(tracklist.to_json(), indent=2))
    atomic_write_text(output_path, tracklist.to_markdown())
    if corrections_db:
        corrections_db.save()


class CorrectionsDB:
    """
    Database for storing and applying user corrections.

    Corrections are stored as mappings from (original_artist, original_title)
    to (corrected_artist, corrected_title). This allows the system to learn
    from user corrections and apply them automatically in future runs.
    """

    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            # $XDG_CONFIG_HOME/setlist-maker/corrections.json, else ~/.config/...
            # -- the same rule as episode.py's config.json, and the one the
            # tests' isolated_user_config redirects.
            db_path = config_dir() / "corrections.json"

        self.db_path = db_path
        self.corrections: dict[str, dict[str, str]] = {}
        # Set when the file exists but could not be read: saving would replace
        # every correction ever learned with only this session's.
        self._unsaveable = False
        self._load()

    def _make_key(self, artist: str, title: str) -> str:
        """Create a normalized key for lookup."""
        return f"{artist.lower().strip()}|||{title.lower().strip()}"

    def _load(self) -> None:
        """Load corrections from disk, never at the cost of the file's contents (#64).

        Unparseable (a half-synced iCloud copy, a stray hand edit) or the wrong
        shape: the file is moved aside to ``corrections.json.corrupt-<time>``
        and this session starts empty, so the next save cannot overwrite it.
        Unreadable (permissions, I/O): it cannot be moved either, so saving is
        refused for this session instead.
        """
        try:
            raw = self.db_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            self._unsaveable = True
            logger.warning(
                "could not read corrections from %s (%s); "
                "corrections from this session will not be saved",
                self.db_path,
                e,
            )
            return
        try:
            data = json.loads(raw)
            corrections = data.get("corrections", {}) if isinstance(data, dict) else None
            if not isinstance(corrections, dict):
                raise ValueError("expected an object with a 'corrections' object")
        except ValueError as e:
            self._quarantine(e)
            return
        self.corrections = corrections

    def _quarantine(self, error: Exception) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        aside = self.db_path.with_name(f"{self.db_path.name}.corrupt-{stamp}")
        try:
            self.db_path.replace(aside)
        except OSError as e:
            self._unsaveable = True
            logger.warning(
                "corrections file %s is unreadable (%s) and could not be moved aside (%s); "
                "corrections from this session will not be saved",
                self.db_path,
                error,
                e,
            )
            return
        logger.warning(
            "corrections file %s is unreadable (%s); kept it as %s and started a new one",
            self.db_path,
            error,
            aside,
        )

    def save(self) -> None:
        """Save corrections to disk (atomically; refused if the file couldn't be read)."""
        if self._unsaveable:
            logger.warning("not saving corrections: %s could not be read", self.db_path)
            return
        atomic_write_text(self.db_path, json.dumps({"corrections": self.corrections}, indent=2))

    def add_correction(
        self,
        original_artist: str,
        original_title: str,
        corrected_artist: str,
        corrected_title: str,
    ) -> None:
        """Record a correction."""
        key = self._make_key(original_artist, original_title)
        self.corrections[key] = {
            "artist": corrected_artist,
            "title": corrected_title,
            "original_artist": original_artist,
            "original_title": original_title,
            "corrected_at": datetime.now().isoformat(),
        }

    def get_correction(self, artist: str, title: str) -> tuple[str, str] | None:
        """Look up a correction for a given artist/title."""
        key = self._make_key(artist, title)
        if key in self.corrections:
            corr = self.corrections[key]
            return corr["artist"], corr["title"]
        return None

    def apply_corrections(self, tracklist: Tracklist) -> int:
        """Apply known corrections to a tracklist. Returns count of corrections applied."""
        applied = 0
        for track in tracklist.tracks:
            # Skip unidentified tracks - no point looking up corrections for empty strings
            if track.is_unidentified:
                continue
            correction = self.get_correction(track.artist, track.title)
            if correction:
                track.original_artist = track.artist
                track.original_title = track.title
                track.artist, track.title = correction
                applied += 1
        return applied
