"""Tests for the keyword classifier."""

import pytest
from app.classifier import classify


class TestClassifyEnglish:
    def test_strong_beirut_explosion(self):
        result = classify(
            original_text="Large explosion heard in Beirut right now",
            english_translation=None,
        )
        assert result.has_location_match is True
        assert result.has_event_match is True
        assert result.location_score > 0
        assert result.event_score > 0
        assert result.raw_keyword_score > 0

    def test_no_location_no_event(self):
        result = classify(
            original_text="The weather in London is nice today.",
            english_translation=None,
        )
        assert result.has_location_match is False
        assert result.has_event_match is False

    def test_speculative_penalty(self):
        result = classify(
            original_text="Maybe there was an explosion in Beirut? Unconfirmed.",
            english_translation=None,
        )
        assert result.has_speculative is True
        assert result.speculative_penalty < 0

    def test_historical_penalty(self):
        result = classify(
            original_text="Anniversary of the Beirut explosion in 2020",
            english_translation=None,
        )
        assert result.has_historical is True
        assert result.historical_penalty < 0

    def test_question_only(self):
        result = classify(
            original_text="Did an explosion happen in Beirut?",
            english_translation=None,
        )
        assert result.is_question_only is True

    def test_dahieh_location(self):
        result = classify(
            original_text="Strike reported in Dahieh",
            english_translation=None,
        )
        assert result.has_location_match is True
        assert result.has_event_match is True

    def test_media_not_counted_in_classification(self):
        # has_media is passed to scoring, not classifier
        result = classify(
            original_text="Beirut explosion",
            english_translation=None,
            has_media=True,
        )
        assert result.has_location_match is True
        assert result.has_event_match is True


class TestClassifyArabic:
    def test_arabic_explosion_beirut(self):
        result = classify(
            original_text="انفجار قوي في بيروت الآن",
            english_translation=None,
        )
        assert result.has_location_match is True
        assert result.has_event_match is True

    def test_arabic_translation_used(self):
        result = classify(
            original_text="نص لا يحتوي على كلمات مفتاحية",
            english_translation="Large explosion reported in Beirut",
        )
        assert result.has_location_match is True
        assert result.has_event_match is True

    def test_arabic_dahiyeh(self):
        result = classify(
            original_text="قصف في الضاحية الجنوبية لبيروت",
            english_translation=None,
        )
        assert result.has_location_match is True
        assert result.has_event_match is True


class TestClassifyHebrew:
    def test_hebrew_explosion_beirut(self):
        result = classify(
            original_text="פיצוץ חזק בביירות עכשיו",
            english_translation=None,
        )
        assert result.has_location_match is True
        assert result.has_event_match is True

    def test_hebrew_airstrike(self):
        result = classify(
            original_text="תקיפת אוויר בביירות",
            english_translation=None,
        )
        assert result.has_location_match is True
        assert result.has_event_match is True


class TestDeduplication:
    def test_same_keyword_not_double_counted(self):
        """Same keyword in both original and translation should not double-score."""
        result_both = classify(
            original_text="Explosion in Beirut",
            english_translation="Explosion in Beirut",  # identical
        )
        result_single = classify(
            original_text="Explosion in Beirut",
            english_translation=None,
        )
        # Both should produce the same score since it's the same keyword
        assert result_both.event_score == result_single.event_score
