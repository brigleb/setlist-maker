"""The episode's own title and artist, written into the MP3 beside its chapters.

Stored per set in ``<set>_episode.json`` beside the tracklist -- a sibling file,
like ``<set>_cover.jpg``, because the JSON sidecar is a bare list of tracks that
must stay one (see CLAUDE.md, "The JSON sidecar's shape").

A set with no file yet gets defaults: the title is the date its name starts
with ("2026-09-23-Keys-Lounge" -> "September 23, 2026"), and the artist is the
last one saved from the editor, remembered in ``~/.config/setlist-maker/
config.json`` so a DJ types their name once. A saved blank means "leave that tag
alone", which is also what a set with no date and no remembered artist gets:
the recorder may already have tagged the file.
"""

import json
import logging
import os
import re
from datetime import date
from pathlib import Path

from setlist_maker.uploads import _write_atomic, set_stem

logger = logging.getLogger(__name__)

# One line in a podcast app's header; nothing longer is a title.
MAX_TAG_LENGTH = 200

_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?!\d)")


def episode_path_for(tracklist_path: Path) -> Path:
    """Where this set's episode title and artist live (may not exist)."""
    return tracklist_path.with_name(f"{set_stem(tracklist_path)}_episode.json")


def config_path() -> Path:
    """The user-level settings file. Honours ``XDG_CONFIG_HOME`` (tests rely on it)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "setlist-maker" / "config.json"


def clean_tag(value: object) -> str:
    """One line, trimmed and capped; anything that is not a string is blank."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_TAG_LENGTH]


def default_title(tracklist_path: Path) -> str:
    """The date a set's name starts with, as "September 23, 2026"; else blank."""
    m = _DATE_PREFIX.match(set_stem(tracklist_path))
    if not m:
        return ""
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return ""
    return f"{d:%B} {d.day}, {d.year}"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("ignoring unreadable %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def remembered_artist() -> str:
    return clean_tag(_read_json(config_path()).get("episode_artist"))


def remember_artist(artist: str) -> None:
    """Make ``artist`` the default for the next set. Best-effort."""
    artist = clean_tag(artist)
    if not artist or artist == remembered_artist():
        return
    path = config_path()
    config = _read_json(path)
    config["episode_artist"] = artist
    try:
        _write_atomic(path, (json.dumps(config, indent=2) + "\n").encode("utf-8"))
    except OSError as e:
        logger.warning("could not remember the episode artist in %s: %s", path, e)


def episode_tags(tracklist_path: Path) -> dict[str, str]:
    """The set's ``{"title", "artist"}``: what was saved, else the defaults.

    Each key falls back on its own, so a hand-edited file missing one still
    gets the other's default -- but a saved ``""`` stays blank.
    """
    saved = _read_json(episode_path_for(tracklist_path))
    return {
        "title": clean_tag(saved["title"]) if "title" in saved else default_title(tracklist_path),
        "artist": clean_tag(saved["artist"]) if "artist" in saved else remembered_artist(),
    }


def save_episode_tags(tracklist_path: Path, title: object, artist: object) -> None:
    """Store the set's title and artist, and remember the artist for the next set."""
    tags = {"title": clean_tag(title), "artist": clean_tag(artist)}
    path = episode_path_for(tracklist_path)
    if _read_json(path) != tags:
        _write_atomic(path, (json.dumps(tags, indent=2) + "\n").encode("utf-8"))
    remember_artist(tags["artist"])
