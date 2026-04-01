"""Tests for the alert dispatch module."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.alerts import _format_early_warning, _format_confirmation, _build_payload
from app.scoring import ScoreResult, ScoreFactor


def _make_score(total=55.0) -> ScoreResult:
    return ScoreResult(
        total=total,
        factors=[
            ScoreFactor("location_keywords", 30.0, "Beirut matched"),
            ScoreFactor("event_keywords", 25.0, "explosion matched"),
        ],
        is_early_warning=total >= 40,
        is_confirmation=total >= 70,
    )


def _make_message(
    raw_text="انفجار في بيروت",
    translation="Explosion in Beirut",
    lang="ar",
    media_type=None,
    source_name="Test Source",
) -> MagicMock:
    msg = MagicMock()
    msg.raw_text = raw_text
    msg.english_translation = translation
    msg.detected_language = lang
    msg.media_type = media_type
    msg.timestamp = datetime(2024, 1, 15, 12, 30, 0)
    msg.source_id = 1
    msg.source = MagicMock()
    msg.source.name = source_name
    return msg


def _make_incident(
    id=1,
    status="early_warning",
    confidence=55.0,
    location="Beirut",
) -> MagicMock:
    inc = MagicMock()
    inc.id = id
    inc.status = status
    inc.confidence_score = confidence
    inc.location_hint = location
    inc.first_seen = datetime(2024, 1, 15, 12, 30, 0)
    inc.last_updated = datetime(2024, 1, 15, 12, 31, 0)
    return inc


class TestFormatEarlyWarning:
    def test_contains_warning_title(self):
        msg = _make_message()
        inc = _make_incident()
        score = _make_score()
        text = _format_early_warning(msg, inc, score, "Test Source")
        assert "EARLY WARNING" in text

    def test_contains_original_text(self):
        msg = _make_message(raw_text="انفجار في بيروت")
        inc = _make_incident()
        score = _make_score()
        text = _format_early_warning(msg, inc, score, "Test Source")
        assert "انفجار في بيروت" in text

    def test_contains_translation(self):
        msg = _make_message(translation="Explosion in Beirut", lang="ar")
        inc = _make_incident()
        score = _make_score()
        text = _format_early_warning(msg, inc, score, "Test Source")
        assert "Explosion in Beirut" in text

    def test_contains_incident_id(self):
        inc = _make_incident(id=42)
        score = _make_score()
        text = _format_early_warning(_make_message(), inc, score, "Source")
        assert "#42" in text

    def test_no_translation_shown_for_english(self):
        msg = _make_message(
            raw_text="Explosion in Beirut", translation="Explosion in Beirut", lang="en"
        )
        inc = _make_incident()
        score = _make_score()
        text = _format_early_warning(msg, inc, score, "Test Source")
        # Translation section should not appear for English messages
        assert "English translation" not in text


class TestFormatConfirmation:
    def test_contains_confirmation_title(self):
        inc = _make_incident(status="confirmed", confidence=75.0)
        msgs = [_make_message()]
        score = _make_score(total=75.0)
        text = _format_confirmation(inc, msgs, score, "multiple sources corroborated")
        assert "CONFIRMED" in text

    def test_contains_reason(self):
        inc = _make_incident()
        text = _format_confirmation(
            inc, [_make_message()], _make_score(), "3 independent sources"
        )
        assert "3 independent sources" in text

    def test_multiple_messages_listed(self):
        inc = _make_incident()
        msgs = [_make_message(source_name=f"Source {i}") for i in range(3)]
        text = _format_confirmation(inc, msgs, _make_score(), "corroborated")
        assert "Source 0" in text
        assert "Source 1" in text


class TestBuildPayload:
    def test_payload_structure(self):
        inc = _make_incident()
        msgs = [_make_message()]
        score = _make_score()
        payload = _build_payload("early_warning", inc, msgs, score, "Test Source", "reason")
        assert payload["alert_level"] == "early_warning"
        assert payload["incident_id"] == 1
        assert "messages" in payload
        assert len(payload["messages"]) == 1
        assert "dashboard_url" in payload

    def test_payload_includes_translation(self):
        msg = _make_message(translation="Explosion in Beirut", lang="ar")
        payload = _build_payload("early_warning", _make_incident(), [msg], _make_score(), "S", "r")
        assert payload["messages"][0]["english_translation"] == "Explosion in Beirut"


class TestSendEarlyWarning:
    @pytest.mark.asyncio
    async def test_dispatches_successfully(self):
        from app import alerts
        # Patch out actual delivery methods
        with patch.object(alerts, "_send_telegram_dm", new_callable=AsyncMock, return_value=True), \
             patch.object(alerts, "_send_webhook", new_callable=AsyncMock, return_value=False), \
             patch.object(alerts, "_send_email", new_callable=AsyncMock, return_value=False), \
             patch.object(alerts, "_send_desktop_notification", return_value=True):
            evt = await alerts.send_early_warning(
                message=_make_message(),
                incident=_make_incident(),
                score_result=_make_score(),
                source_name="Test Source",
            )
        assert evt.alert_level == "early_warning"
        assert evt.success is True
        assert "telegram_dm" in evt.sent_to
