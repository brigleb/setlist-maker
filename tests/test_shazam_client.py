"""Tests for setlist_maker.shazam_client."""

import asyncio
import tempfile
from pathlib import Path

from shazamio.exceptions import FailedDecodeJson

from setlist_maker.shazam_client import estimate_confidence, identify_sample_with_retry


class TestEstimateConfidence:
    """Tests for the heuristic match-confidence proxy."""

    def test_no_matches_is_neutral(self):
        """A track with no match detail gets a neutral score."""
        assert estimate_confidence({"track": {}}) == 0.5

    def test_well_aligned_matches_score_high(self):
        """Several well-aligned matches (low skew) score above a single one."""
        strong = estimate_confidence(
            {"matches": [{"frequencyskew": 0.0}, {"frequencyskew": 0.01}, {"frequencyskew": 0.0}]}
        )
        weak = estimate_confidence({"matches": [{"frequencyskew": 0.0}]})
        assert strong > weak
        assert 0.0 <= weak <= 1.0
        assert 0.0 <= strong <= 1.0

    def test_high_skew_lowers_score(self):
        """Poorly aligned matches (high frequency skew) score lower."""
        aligned = estimate_confidence({"matches": [{"frequencyskew": 0.0}]})
        skewed = estimate_confidence({"matches": [{"frequencyskew": 0.9}]})
        assert skewed < aligned


class TestErrorReporting:
    """`identify_sample_with_retry` swallows every non-rate-limit exception and
    returns None, which is indistinguishable from a genuine no-match. The
    optional `on_error` hook is how the call log recovers what was lost."""

    def _run(self, recognize, **kwargs):
        class FakeShazam:
            async def recognize(self, path):
                return recognize()

        class FakeSegment:
            def export(self, path, format):
                Path(path).write_bytes(b"")

        with tempfile.TemporaryDirectory() as temp_dir:
            return asyncio.run(
                identify_sample_with_retry(FakeShazam(), FakeSegment(), temp_dir, **kwargs)
            )

    def test_a_failed_recognition_is_reported_to_on_error(self):
        seen = []

        def boom():
            raise FailedDecodeJson("Failed to decode json")

        result = self._run(boom, on_error=seen.append)

        assert result is None  # behaviour unchanged
        assert [type(e).__name__ for e in seen] == ["FailedDecodeJson"]

    def test_a_successful_recognition_reports_no_error(self):
        seen = []
        result = self._run(
            lambda: {"track": {"title": "Autobahn", "subtitle": "Kraftwerk"}},
            on_error=seen.append,
        )
        assert result["title"] == "Autobahn"
        assert seen == []

    def test_on_error_is_optional(self):
        def boom():
            raise ValueError("nope")

        assert self._run(boom) is None
