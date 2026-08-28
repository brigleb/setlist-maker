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
