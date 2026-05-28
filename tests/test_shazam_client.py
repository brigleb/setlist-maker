"""Tests for setlist_maker.shazam_client."""

from setlist_maker.shazam_client import estimate_confidence


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
