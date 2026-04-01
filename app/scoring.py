"""Transparent weighted scoring system.

All factors are visible in the returned ScoreResult so alerts can explain
exactly why a message triggered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app import config
from app.classifier import ClassificationResult
from app.trust import tier_score_bonus


@dataclass
class ScoreFactor:
    name: str
    value: float
    reason: str


@dataclass
class ScoreResult:
    total: float
    factors: list[ScoreFactor] = field(default_factory=list)
    is_early_warning: bool = False
    is_confirmation: bool = False

    def breakdown_text(self) -> str:
        lines = [f"Total score: {self.total:.1f}"]
        for f in self.factors:
            sign = "+" if f.value >= 0 else ""
            lines.append(f"  {sign}{f.value:.1f}  {f.name}: {f.reason}")
        return "\n".join(lines)

    def matched_keywords(self) -> list[str]:
        """Return keyword strings from classification hits (stored on factors)."""
        return [f.name for f in self.factors if f.name.startswith("keyword:")]


def score_message(
    classification: ClassificationResult,
    trust_tier: int,
    has_media: bool,
    is_forward: bool,
) -> ScoreResult:
    """Compute a relevance score and check thresholds.

    Returns a ScoreResult with individual factor breakdown.
    """
    factors: list[ScoreFactor] = []
    total = 0.0

    # ── Keyword scores ────────────────────────────────────────────────────────
    if classification.location_score > 0:
        factors.append(
            ScoreFactor(
                "location_keywords",
                classification.location_score,
                f"{classification.location_score:.1f} pts from location matches",
            )
        )
        total += classification.location_score

    if classification.event_score > 0:
        factors.append(
            ScoreFactor(
                "event_keywords",
                classification.event_score,
                f"{classification.event_score:.1f} pts from event/incident wording",
            )
        )
        total += classification.event_score

    if classification.time_score > 0:
        factors.append(
            ScoreFactor(
                "time_keywords",
                classification.time_score,
                "current-time phrasing detected",
            )
        )
        total += classification.time_score

    # ── Source trust bonus ────────────────────────────────────────────────────
    bonus = tier_score_bonus(trust_tier)
    if bonus:
        factors.append(
            ScoreFactor(
                "trust_tier_bonus",
                bonus,
                f"Tier {trust_tier} source bonus",
            )
        )
        total += bonus

    # ── Media evidence bonus ─────────────────────────────────────────────────
    if has_media:
        factors.append(
            ScoreFactor(
                "media_evidence",
                config.MEDIA_BONUS,
                "message contains media (photo/video)",
            )
        )
        total += config.MEDIA_BONUS

    # ── Specificity bonus ─────────────────────────────────────────────────────
    if classification.has_location_match and classification.has_event_match:
        factors.append(
            ScoreFactor(
                "specificity_bonus",
                config.SPECIFICITY_BONUS,
                "both location and event wording present",
            )
        )
        total += config.SPECIFICITY_BONUS

    # ── Penalties ─────────────────────────────────────────────────────────────
    if classification.speculative_penalty < 0:
        factors.append(
            ScoreFactor(
                "speculative_penalty",
                classification.speculative_penalty,
                "speculative / unconfirmed language detected",
            )
        )
        total += classification.speculative_penalty

    if classification.historical_penalty < 0:
        factors.append(
            ScoreFactor(
                "historical_penalty",
                classification.historical_penalty,
                "historical reference or date found",
            )
        )
        total += classification.historical_penalty

    if is_forward:
        factors.append(
            ScoreFactor(
                "forward_penalty",
                config.FORWARD_PENALTY,
                "forwarded message",
            )
        )
        total += config.FORWARD_PENALTY

    if classification.is_question_only:
        factors.append(
            ScoreFactor(
                "question_only_penalty",
                config.QUESTION_ONLY_PENALTY,
                "message appears to be a question only",
            )
        )
        total += config.QUESTION_ONLY_PENALTY

    total = max(total, 0.0)

    result = ScoreResult(total=round(total, 2), factors=factors)
    result.is_early_warning = total >= config.EARLY_WARNING_THRESHOLD
    result.is_confirmation = total >= config.CONFIRMATION_THRESHOLD
    return result


def score_incident(messages_scores: list[float], unique_source_count: int) -> float:
    """Compute a combined incident confidence score from individual message scores."""
    if not messages_scores:
        return 0.0
    base = max(messages_scores)
    corroboration = (unique_source_count - 1) * config.CORROBORATION_BONUS
    return round(min(base + corroboration, 100.0), 2)
