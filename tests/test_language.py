"""Tests for language detection."""

import pytest
from app.language import detect_language, is_arabic, is_hebrew, needs_translation


class TestDetectLanguage:
    def test_arabic_text(self):
        text = "انفجار قوي في بيروت الآن"
        result = detect_language(text)
        assert result == "ar", f"Expected 'ar', got '{result}'"

    def test_hebrew_text(self):
        text = "פיצוץ חזק בביירות עכשיו"
        result = detect_language(text)
        assert result == "he", f"Expected 'he', got '{result}'"

    def test_english_text(self):
        text = "Large explosion reported in Beirut"
        result = detect_language(text)
        assert result == "en", f"Expected 'en', got '{result}'"

    def test_empty_string(self):
        result = detect_language("")
        assert result == "unknown"

    def test_whitespace_only(self):
        result = detect_language("   \n  ")
        assert result == "unknown"

    def test_arabic_mixed_with_numbers(self):
        text = "انفجار في بيروت 2024"
        result = detect_language(text)
        assert result == "ar"

    def test_hebrew_with_url(self):
        text = "פיצוץ https://example.com ביירות"
        result = detect_language(text)
        assert result == "he"

    def test_arabic_with_english_names(self):
        # Arabic with Beirut spelled in English – should still be detected as AR
        text = "انفجار في Beirut الآن"
        result = detect_language(text)
        assert result == "ar"


class TestIsArabic:
    def test_arabic_true(self):
        assert is_arabic("بيروت") is True

    def test_english_false(self):
        assert is_arabic("Beirut") is False

    def test_empty_false(self):
        assert is_arabic("") is False


class TestIsHebrew:
    def test_hebrew_true(self):
        assert is_hebrew("ביירות") is True

    def test_arabic_false(self):
        assert is_hebrew("بيروت") is False


class TestNeedsTranslation:
    def test_arabic_needs(self):
        assert needs_translation("ar") is True

    def test_hebrew_needs(self):
        assert needs_translation("he") is True

    def test_english_no(self):
        assert needs_translation("en") is False

    def test_unknown_no(self):
        assert needs_translation("unknown") is False
