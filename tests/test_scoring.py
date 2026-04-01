"""Tests for the scoring system."""

import pytest
from app.classifier import classify
from app.scoring import score_message, score_incident
from app import config


class TestScoreMessage:
    def _score(self, text, tier=3, has_media=False, is_forward=False, translation=None):
        cl = classify(text, translation)
        return score_message(cl, trust_tier=tier, has_media=has_media, is_forward=is_forward)

    def test_strong_early_warning(self):
        result = self._score("Explosion in Beirut right now", tier=2)
        assert result.total >= config.EARLY_WARNING_THRESHOLD
        assert result.is_early_warning is True

    def test_low_score_no_beirut(self):
        result = self._score("Earthquake in Tokyo")
        assert result.total < config.EARLY_WARNING_THRESHOLD
        assert result.is_early_warning is False

    def test_media_bonus_applied(self):
        no_media = self._score("Explosion in Beirut", tier=3, has_media=False)
        with_media = self._score("Explosion in Beirut", tier=3, has_media=True)
        assert with_media.total == no_media.total + config.MEDIA_BONUS

    def test_tier_1_bonus(self):
        t1 = self._score("Explosion in Beirut", tier=1)
        t3 = self._score("Explosion in Beirut", tier=3)
        expected_diff = config.TRUST_TIER_BONUS[1] - config.TRUST_TIER_BONUS[3]
        assert abs(t1.total - t3.total - expected_diff) < 0.1

    def test_forward_penalty(self):
        normal = self._score("Explosion in Beirut", tier=3, is_forward=False)
        forwarded = self._score("Explosion in Beirut", tier=3, is_forward=True)
        assert forwarded.total == max(0, normal.total + config.FORWARD_PENALTY)

    def test_confirmation_threshold(self):
        # High-quality signal: location + event + media + tier 1 + time phrase
        result = self._score("Breaking: major airstrike explosion in Beirut now", tier=1, has_media=True)
        assert result.is_confirmation is True

    def test_speculative_reduces_score(self):
        certain = self._score("Explosion in Beirut", tier=3)
        speculative = self._score("Maybe unconfirmed explosion in Beirut", tier=3)
        assert speculative.total < certain.total

    def test_score_breakdown_text(self):
        cl = classify("Explosion in Beirut now", None)
        result = score_message(cl, trust_tier=2, has_media=False, is_forward=False)
        breakdown = result.breakdown_text()
        assert "Total score:" in breakdown
        assert "location_keywords" in breakdown or "event_keywords" in breakdown

    def test_score_minimum_zero(self):
        cl = classify("maybe unconfirmed rumour historical 2020 anniversary", None)
        result = score_message(cl, trust_tier=3, has_media=False, is_forward=True)
        assert result.total >= 0.0

    def test_arabic_scores(self):
        result = self._score("انفجار قوي في بيروت الآن", tier=2)
        assert result.total >= config.EARLY_WARNING_THRESHOLD
        assert result.is_early_warning is True


class TestScoreIncident:
    def test_single_message(self):
        score = score_incident([50.0], unique_source_count=1)
        assert score == 50.0

    def test_corroboration_increases_score(self):
        single = score_incident([50.0], unique_source_count=1)
        multi = score_incident([50.0, 45.0], unique_source_count=2)
        assert multi > single

    def test_capped_at_100(self):
        score = score_incident([95.0, 90.0, 85.0], unique_source_count=10)
        assert score <= 100.0

    def test_empty_messages(self):
        score = score_incident([], unique_source_count=0)
        assert score == 0.0
