"""
Which Shazam answers name the same track.

Normalizes an (artist, title) pair -- dropping remix/feat/edit tags, brackets
and punctuation -- and clusters keys whose artist and title are each similar
enough. The sequential dedup pipeline (``identify.py``) and the adaptive engine
(``boundary.py``) both use it, so the same track under two labels never
manufactures a boundary. Standard library only: ``boundary.py`` is pure, and
importing this must not pull in the Shazam client or the TUI.
"""

import re
from difflib import SequenceMatcher

# Tokens that describe a *version* of a track rather than its identity. Stripped
# before comparison so "Song" and "Song - Radio Edit" cluster together.
_VERSION_TOKENS_RE = re.compile(
    r"\b(radio edit|original mix|extended mix|club mix|remaster(?:ed)?|remix|"
    r"re-?edit|edit|version|vip|bootleg|mono|stereo|live|acoustic|instrumental)\b",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\b(feat\.?|ft\.?|featuring|with)\b.*", re.IGNORECASE)
_BRACKETED_RE = re.compile(r"[(\[{].*?[)\]}]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_text(text: str) -> str:
    """Lowercase and strip version/feature tags and punctuation for matching."""
    text = text.lower()
    text = _BRACKETED_RE.sub(" ", text)
    text = _FEAT_RE.sub(" ", text)
    text = _VERSION_TOKENS_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def normalized_key(track_info: dict) -> tuple[str, str]:
    """Build a normalized (artist, title) key for fuzzy comparison."""
    return (
        _normalize_text(track_info.get("artist", "")),
        _normalize_text(track_info.get("title", "")),
    )


def assign_cluster(
    key: tuple[str, str],
    clusters: list[tuple[str, str]],
    title_threshold: float,
    artist_threshold: float,
) -> tuple[str, str]:
    """Return the representative key for `key`, fuzzily matched against clusters.

    Greedy and order-preserving: a cluster matches when the artist is within
    `artist_threshold` and the title is within `title_threshold`; the first such
    cluster wins, otherwise `key` becomes a new cluster representative.
    """
    artist, title = key
    for representative in clusters:
        rep_artist, rep_title = representative
        if (
            SequenceMatcher(None, artist, rep_artist).ratio() >= artist_threshold
            and SequenceMatcher(None, title, rep_title).ratio() >= title_threshold
        ):
            return representative
    clusters.append(key)
    return key
