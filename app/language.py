"""Language detection module.

Primary: langdetect library.
Fallback: Unicode character-range heuristic (Arabic / Hebrew / Latin).

Returns ISO 639-1 language codes: "ar", "he", "en", or "unknown".
"""

from __future__ import annotations

import logging
import re
import unicodedata

log = logging.getLogger(__name__)

# Try to import langdetect; fall back gracefully if not installed
try:
    from langdetect import detect, LangDetectException
    from langdetect import DetectorFactory
    DetectorFactory.seed = 42  # make detection deterministic
    _LANGDETECT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _LANGDETECT_AVAILABLE = False

# Unicode block ranges
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_HEBREW_RE = re.compile(r"[\u0590-\u05FF\uFB1D-\uFB4F]")


def _heuristic_detect(text: str) -> str | None:
    """Character-set heuristic.  Returns 'ar', 'he', 'en', or None."""
    if not text:
        return None
    arabic_count = len(_ARABIC_RE.findall(text))
    hebrew_count = len(_HEBREW_RE.findall(text))
    latin_count = sum(1 for c in text if "LATIN" in unicodedata.name(c, ""))
    total = arabic_count + hebrew_count + latin_count
    if total == 0:
        return None
    if arabic_count / total > 0.3:
        return "ar"
    if hebrew_count / total > 0.3:
        return "he"
    if latin_count / total > 0.5:
        return "en"
    return None


def detect_language(text: str) -> str:
    """Detect the primary language of *text*.

    Returns a two-letter ISO code: "ar", "he", "en", or "unknown".
    """
    if not text or not text.strip():
        return "unknown"

    cleaned = text.strip()

    # Heuristic first (fast, reliable for Arabic / Hebrew scripts)
    heuristic = _heuristic_detect(cleaned)
    if heuristic in ("ar", "he"):
        return heuristic

    # langdetect
    if _LANGDETECT_AVAILABLE:
        try:
            lang = detect(cleaned)
            # Normalise: langdetect returns 'iw' for Hebrew historically
            if lang == "iw":
                lang = "he"
            if lang in ("ar", "he", "en"):
                return lang
            # For other detected languages, trust heuristic if present
            if heuristic:
                return heuristic
            return lang
        except Exception:  # LangDetectException or any other
            pass

    # Fall back to heuristic result or unknown
    return heuristic or "unknown"


def is_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text))


def is_hebrew(text: str) -> bool:
    return bool(_HEBREW_RE.search(text))


def needs_translation(lang: str) -> bool:
    """Return True if the text should be translated to English."""
    return lang in ("ar", "he") or (lang not in ("en", "unknown") and lang != "")
