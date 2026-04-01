"""Application configuration.

Values are loaded from environment variables (via python-dotenv) and
from sources.yaml.  All keyword lists and thresholds live here so they
can be changed without touching business logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Load .env from project root
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


# ─── Telegram ────────────────────────────────────────────────────────────────

TELEGRAM_API_ID: int = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE: str = os.getenv("TELEGRAM_PHONE", "")
TELEGRAM_SESSION_NAME: str = os.getenv("TELEGRAM_SESSION_NAME", "beirut_monitor")
TELEGRAM_SESSION_PATH: str = str(_ROOT / "data" / TELEGRAM_SESSION_NAME)

ALERT_TELEGRAM_CHAT_ID: int | None = (
    int(v) if (v := os.getenv("ALERT_TELEGRAM_CHAT_ID")) else None
)

# ─── Dashboard ───────────────────────────────────────────────────────────────

DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8080"))
DASHBOARD_USERNAME: str = os.getenv("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")

# ─── Database ────────────────────────────────────────────────────────────────

DB_URL: str = os.getenv(
    "DB_URL", f"sqlite+aiosqlite:///{_ROOT / 'data' / 'incidents.db'}"
)

# ─── Translation ─────────────────────────────────────────────────────────────

TRANSLATION_PROVIDER: str = os.getenv("TRANSLATION_PROVIDER", "google")
DEEPL_API_KEY: str = os.getenv("DEEPL_API_KEY", "")
LIBRE_TRANSLATE_URL: str = os.getenv("LIBRE_TRANSLATE_URL", "http://localhost:5000")

# ─── Thresholds ──────────────────────────────────────────────────────────────

EARLY_WARNING_THRESHOLD: float = float(os.getenv("EARLY_WARNING_THRESHOLD", "40"))
CONFIRMATION_THRESHOLD: float = float(os.getenv("CONFIRMATION_THRESHOLD", "70"))
INCIDENT_CLUSTER_WINDOW_SECONDS: int = int(
    os.getenv("INCIDENT_CLUSTER_WINDOW_SECONDS", "1800")
)

# ─── Notifications ───────────────────────────────────────────────────────────

WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

SMTP_HOST: str = os.getenv("SMTP_HOST", "")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
ALERT_EMAIL: str = os.getenv("ALERT_EMAIL", "")

# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ─── Keyword lists ───────────────────────────────────────────────────────────

# Each entry: (keyword_text, score_weight)
LOCATION_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "en": [
        ("beirut", 30.0),
        ("dahieh", 25.0),
        ("dahiyeh", 25.0),
        ("dahye", 25.0),
        ("southern beirut", 28.0),
        ("south beirut", 28.0),
        ("beirut suburb", 25.0),
        ("southern suburb", 20.0),
    ],
    "ar": [
        ("بيروت", 30.0),
        ("الضاحية", 25.0),
        ("ضاحية بيروت", 28.0),
        ("الضاحية الجنوبية", 27.0),
        ("جنوب بيروت", 27.0),
        ("بيروت الجنوبية", 27.0),
    ],
    "he": [
        ("ביירות", 30.0),
        ("בביירות", 30.0),     # "in Beirut" – preposition attached
        ("לביירות", 28.0),     # "to Beirut"
        ("מביירות", 28.0),     # "from Beirut"
        ("פרבר דרומי", 25.0),
        ("הדאחיה", 25.0),
        ("בדאחיה", 25.0),
        ("דאחיה", 25.0),
        ("פרברי ביירות", 24.0),
        ("דרום ביירות", 27.0),
    ],
}

EVENT_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "en": [
        ("explosion", 25.0),
        ("blast", 25.0),
        ("airstrike", 28.0),
        ("air strike", 28.0),
        ("strike", 20.0),
        ("attack", 20.0),
        ("bombing", 25.0),
        ("bomb", 22.0),
        ("missile", 25.0),
        ("rocket", 22.0),
        ("raid", 22.0),
        ("hit", 12.0),
        ("impact", 15.0),
        ("smoke", 12.0),
        ("fire", 10.0),
        ("loud blast", 28.0),
        ("heard explosion", 28.0),
        ("reported explosion", 25.0),
        ("shelling", 22.0),
        ("gunfire", 18.0),
        ("targeted", 20.0),
    ],
    "ar": [
        ("انفجار", 25.0),
        ("قصف", 25.0),
        ("غارة", 25.0),
        ("هجوم", 20.0),
        ("صاروخ", 25.0),
        ("دخان", 12.0),
        ("حريق", 10.0),
        ("سمع دوي انفجار", 28.0),
        ("استهداف", 22.0),
        ("غارة على بيروت", 30.0),
        ("قصف بيروت", 30.0),
        ("طائرة مسيّرة", 22.0),
        ("مسيّرة", 20.0),
        ("ضربة", 22.0),
        ("قذيفة", 22.0),
        ("مدفعية", 20.0),
        ("تفجير", 25.0),
    ],
    "he": [
        ("פיצוץ", 25.0),
        ("תקיפה", 25.0),
        ("הפצצה", 25.0),
        ("טיל", 25.0),
        ("עשן", 12.0),
        ("שריפה", 10.0),
        ("נשמע פיצוץ", 28.0),
        ("דווח על פיצוץ", 25.0),
        ("תקיפה בביירות", 30.0),
        ("פגיעה", 22.0),
        ("תקיפת אוויר", 28.0),
        ("כטב\"ם", 22.0),
        ("רקטה", 22.0),
        ("ירי", 18.0),
        ("כוחות", 15.0),
    ],
}

CURRENT_TIME_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "en": [
        ("now", 8.0),
        ("just now", 10.0),
        ("breaking", 12.0),
        ("just heard", 10.0),
        ("right now", 10.0),
        ("moments ago", 10.0),
        ("tonight", 8.0),
        ("this morning", 8.0),
        ("today", 6.0),
        ("ongoing", 10.0),
        ("live", 8.0),
        ("currently", 8.0),
        ("happening now", 12.0),
    ],
    "ar": [
        ("الآن", 8.0),
        ("للتو", 10.0),
        ("عاجل", 12.0),
        ("مباشر", 10.0),
        ("قبل قليل", 10.0),
        ("اليوم", 6.0),
        ("اللحظة", 10.0),
        ("مستمر", 10.0),
    ],
    "he": [
        ("עכשיו", 8.0),
        ("הרגע", 10.0),
        ("כרגע", 10.0),
        ("בזמן אמת", 10.0),
        ("היום", 6.0),
        ("מתמשך", 10.0),
        ("פורץ", 12.0),
    ],
}

SPECULATIVE_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "en": [
        ("maybe", -15.0),
        ("unconfirmed", -12.0),
        ("hearing that", -10.0),
        ("rumor", -15.0),
        ("rumours", -15.0),
        ("reportedly", -5.0),
        ("allegedly", -8.0),
        ("could be", -10.0),
        ("might be", -10.0),
        ("not confirmed", -15.0),
        ("sources say", -5.0),
    ],
    "ar": [
        ("غير مؤكد", -12.0),
        ("شائعة", -15.0),
        ("يقال", -8.0),
        ("ربما", -10.0),
        ("على ما يبدو", -8.0),
    ],
    "he": [
        ("לא מאושר", -12.0),
        ("שמועה", -15.0),
        ("אולי", -10.0),
        ("כנראה", -8.0),
        ("לפי הדיווחים", -5.0),
    ],
}

HISTORICAL_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "en": [
        ("in 2020", -20.0),
        ("in 2006", -20.0),
        ("years ago", -20.0),
        ("anniversary", -20.0),
        ("historical", -20.0),
        ("remember when", -20.0),
        ("last year", -15.0),
        ("last month", -10.0),
        ("archive", -18.0),
    ],
    "ar": [
        ("في عام", -15.0),
        ("منذ سنوات", -20.0),
        ("الذكرى", -18.0),
        ("تاريخي", -20.0),
    ],
    "he": [
        ("לפני שנים", -20.0),
        ("זיכרון", -18.0),
        ("היסטורי", -20.0),
    ],
}

MEDIA_BONUS: float = 15.0
FORWARD_PENALTY: float = -5.0
QUESTION_ONLY_PENALTY: float = -10.0
SPECIFICITY_BONUS: float = 5.0

# Trust-tier score bonuses
TRUST_TIER_BONUS: dict[int, float] = {
    1: 20.0,
    2: 10.0,
    3: 0.0,
}

# Multi-source corroboration bonus (per additional independent source)
CORROBORATION_BONUS: float = 10.0


# ─── Sources ─────────────────────────────────────────────────────────────────

@dataclass
class SourceConfig:
    name: str
    username: str | None = None
    chat_id: str | None = None
    trust_tier: int = 3
    language: str = "mixed"
    enabled: bool = True
    notes: str = ""


def load_sources(path: str | None = None) -> list[SourceConfig]:
    """Load source definitions from sources.yaml."""
    sources_file = Path(path or (_ROOT / "sources.yaml"))
    if not sources_file.exists():
        return []
    with sources_file.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    result: list[SourceConfig] = []
    for entry in data.get("sources", []):
        result.append(
            SourceConfig(
                name=entry.get("name", "Unknown"),
                username=entry.get("username"),
                chat_id=str(entry["chat_id"]) if entry.get("chat_id") else None,
                trust_tier=int(entry.get("trust_tier", 3)),
                language=entry.get("language", "mixed"),
                enabled=bool(entry.get("enabled", True)),
                notes=entry.get("notes", ""),
            )
        )
    return result
