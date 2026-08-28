"""Offset capture on identify_sample_with_retry (adaptive engine's raw material)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from setlist_maker.shazam_client import identify_sample_with_retry

RAW = {
    "matches": [{"id": "1", "offset": 154.2, "timeskew": 0.0007, "frequencyskew": 0.001}],
    "track": {"title": "One More Time", "subtitle": "Daft Punk", "url": "u", "images": {}},
}


def _fake_shazam(raw):
    shazam = MagicMock()
    shazam.recognize = AsyncMock(return_value=raw)
    return shazam


def _segment():
    seg = MagicMock()
    seg.export = MagicMock()
    return seg


def test_offsets_included_when_requested(tmp_path):
    info = asyncio.run(
        identify_sample_with_retry(
            _fake_shazam(RAW), _segment(), str(tmp_path), include_offsets=True
        )
    )
    assert info["offsets"] == [{"offset": 154.2, "timeskew": 0.0007}]


def test_offsets_absent_by_default(tmp_path):
    info = asyncio.run(identify_sample_with_retry(_fake_shazam(RAW), _segment(), str(tmp_path)))
    assert "offsets" not in info


def test_matches_without_offset_are_skipped(tmp_path):
    raw = {"matches": [{"id": "1"}], "track": RAW["track"]}
    info = asyncio.run(
        identify_sample_with_retry(
            _fake_shazam(raw), _segment(), str(tmp_path), include_offsets=True
        )
    )
    assert info["offsets"] == []


def _track(**over):
    t = {"title": "One More Time", "subtitle": "Daft Punk", "url": "u", "images": {}}
    t.update(over)
    return t


def test_empty_album_metadata_does_not_discard_the_match(tmp_path):
    """Shazam returns `metadata: []` in the wild. `.get(key, default)` takes the
    key's own empty list, not the default, so indexing it raised IndexError --
    which the blanket handler turned into "not identified", throwing away a
    track Shazam had actually named."""
    raw = {"matches": RAW["matches"], "track": _track(sections=[{"metadata": []}])}
    info = asyncio.run(identify_sample_with_retry(_fake_shazam(raw), _segment(), str(tmp_path)))
    assert info is not None, "a successful match must survive an empty album section"
    assert info["title"] == "One More Time"
    assert info["album"] is None


def test_album_is_read_when_present(tmp_path):
    raw = {
        "matches": RAW["matches"],
        "track": _track(sections=[{"metadata": [{"text": "Discovery"}]}]),
    }
    info = asyncio.run(identify_sample_with_retry(_fake_shazam(raw), _segment(), str(tmp_path)))
    assert info["album"] == "Discovery"


def test_missing_or_empty_sections_are_tolerated(tmp_path):
    for sections in ([], [{}], [None], None):
        raw = {"matches": RAW["matches"], "track": _track(sections=sections)}
        info = asyncio.run(identify_sample_with_retry(_fake_shazam(raw), _segment(), str(tmp_path)))
        assert info is not None and info["album"] is None
