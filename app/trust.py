"""Trust-tier utilities.

Trust tiers are defined per-source in sources.yaml:
  Tier 1 – Official / institutional / highly reliable
  Tier 2 – Established OSINT / war-monitoring channels
  Tier 3 – Fast rumor / community channels

Lower numbers = more trusted.
"""

from __future__ import annotations

from app import config


TIER_LABELS: dict[int, str] = {
    1: "Official / Institutional",
    2: "Established OSINT",
    3: "Community / Rumor",
}


def tier_score_bonus(trust_tier: int) -> float:
    """Return the score bonus for a given trust tier."""
    return config.TRUST_TIER_BONUS.get(trust_tier, 0.0)


def tier_label(trust_tier: int) -> str:
    return TIER_LABELS.get(trust_tier, f"Tier {trust_tier}")


def tier_color(trust_tier: int) -> str:
    """Return a CSS/display color for the tier badge."""
    return {
        1: "#2ecc71",   # green
        2: "#f39c12",   # amber
        3: "#e74c3c",   # red
    }.get(trust_tier, "#95a5a6")
