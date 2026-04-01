"""Keyword-based relevance classifier.

Scans both the original text and the English translation.
Returns a structured ClassificationResult with matched keywords,
boolean flags, and a raw keyword score (before source/media bonuses).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import config
from app.normalize import normalize


@dataclass
class KeywordHit:
    keyword: str
    category: str        # location | event | time | speculative | historical
    language: str
    score: float
    matched_in: str      # "original" | "translation"


@dataclass
class ClassificationResult:
    has_location_match: bool = False
    has_event_match: bool = False
    has_time_match: bool = False
    has_speculative: bool = False
    has_historical: bool = False
    is_question_only: bool = False

    location_score: float = 0.0
    event_score: float = 0.0
    time_score: float = 0.0
    speculative_penalty: float = 0.0
    historical_penalty: float = 0.0

    hits: list[KeywordHit] = field(default_factory=list)

    @property
    def raw_keyword_score(self) -> float:
        total = (
            self.location_score
            + self.event_score
            + self.time_score
            + self.speculative_penalty
            + self.historical_penalty
        )
        # Cap location + event contribution to avoid runaway scores
        return max(total, 0.0)


def _match_keywords(
    text: str,
    keyword_dict: dict[str, list[tuple[str, float]]],
    category: str,
    matched_in: str,
    seen: set[str],
) -> list[KeywordHit]:
    """Match all keyword lists in *keyword_dict* against normalised *text*."""
    hits: list[KeywordHit] = []
    norm = normalize(text)
    for lang, pairs in keyword_dict.items():
        for kw, score in pairs:
            kw_lower = kw.lower()
            # Use word-boundary matching where possible
            pattern = re.compile(
                r"(?<!\w)" + re.escape(kw_lower) + r"(?!\w)",
                re.UNICODE,
            )
            if pattern.search(norm):
                # Dedup by keyword only (not by matched_in) so the same keyword
                # appearing in both original text and its translation counts once.
                dedup_key = f"{category}:{kw_lower}"
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    hits.append(
                        KeywordHit(
                            keyword=kw,
                            category=category,
                            language=lang,
                            score=score,
                            matched_in=matched_in,
                        )
                    )
    return hits


def classify(
    original_text: str,
    english_translation: str | None,
    has_media: bool = False,
    is_forward: bool = False,
) -> ClassificationResult:
    """Run full keyword classification on a message.

    Scans both original and translated text; deduplicates identical hits.
    """
    result = ClassificationResult()
    seen: set[str] = set()

    texts = [("original", original_text or "")]
    if english_translation and english_translation.strip():
        texts.append(("translation", english_translation))

    for matched_in, text in texts:
        # Location keywords
        for hit in _match_keywords(
            text, config.LOCATION_KEYWORDS, "location", matched_in, seen
        ):
            result.has_location_match = True
            result.location_score += hit.score
            result.hits.append(hit)

        # Event keywords
        for hit in _match_keywords(
            text, config.EVENT_KEYWORDS, "event", matched_in, seen
        ):
            result.has_event_match = True
            result.event_score += hit.score
            result.hits.append(hit)

        # Time keywords
        for hit in _match_keywords(
            text, config.CURRENT_TIME_KEYWORDS, "time", matched_in, seen
        ):
            result.has_time_match = True
            result.time_score += hit.score
            result.hits.append(hit)

        # Speculative keywords (penalties)
        for hit in _match_keywords(
            text, config.SPECULATIVE_KEYWORDS, "speculative", matched_in, seen
        ):
            result.has_speculative = True
            result.speculative_penalty += hit.score  # scores are already negative
            result.hits.append(hit)

        # Historical keywords (penalties)
        for hit in _match_keywords(
            text, config.HISTORICAL_KEYWORDS, "historical", matched_in, seen
        ):
            result.has_historical = True
            result.historical_penalty += hit.score
            result.hits.append(hit)

    # Cap location and event scores individually to prevent a single text
    # from dominating the final score via many near-synonyms
    result.location_score = min(result.location_score, 35.0)
    result.event_score = min(result.event_score, 35.0)
    result.time_score = min(result.time_score, 15.0)

    # Question-only check (English only for now)
    combined = " ".join(t for _, t in texts)
    result.is_question_only = _is_question_only(combined)

    return result


def _is_question_only(text: str) -> bool:
    """True if the text consists only of questions with no assertive sentences."""
    stripped = text.strip()
    if not stripped:
        return False
    # Split on sentence-ending punctuation
    sentences = re.split(r"[.!؟。]\s*", stripped)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return False
    return all(
        s.endswith("?") or "؟" in s or s.startswith("?")
        for s in sentences
    )
