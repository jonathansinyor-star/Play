"""Tests for incident clustering and deduplication."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from app.clustering import _guess_location, _determine_status, is_near_duplicate


class TestGuessLocation:
    def test_dahieh_english(self):
        msg = MagicMock()
        msg.raw_text = "Attack in Dahieh area"
        msg.english_translation = ""
        assert "Dahieh" in _guess_location(msg)

    def test_south_beirut(self):
        msg = MagicMock()
        msg.raw_text = "Strike in south Beirut"
        msg.english_translation = ""
        assert "Southern" in _guess_location(msg)

    def test_dahiyeh_arabic(self):
        msg = MagicMock()
        msg.raw_text = "انفجار في الضاحية"
        msg.english_translation = ""
        assert "Dahieh" in _guess_location(msg)

    def test_generic_beirut(self):
        msg = MagicMock()
        msg.raw_text = "Explosion in Beirut"
        msg.english_translation = ""
        loc = _guess_location(msg)
        assert "Beirut" in loc


class TestDetermineStatus:
    def test_high_confidence_confirmed(self):
        from app import config
        status = _determine_status(config.CONFIRMATION_THRESHOLD + 1, 1)
        assert status == "confirmed"

    def test_multiple_messages_monitoring(self):
        status = _determine_status(30.0, 2)
        assert status == "monitoring"

    def test_three_messages_confirmed(self):
        status = _determine_status(30.0, 3)
        assert status == "confirmed"

    def test_single_message_early_warning(self):
        status = _determine_status(45.0, 1)
        assert status == "early_warning"


class TestIsNearDuplicate:
    @pytest.mark.asyncio
    async def test_identical_text_is_duplicate(self):
        """Near-identical text should be flagged."""
        text = "Large explosion reported in Beirut right now"
        mock_msg = MagicMock()
        mock_msg.raw_text = text

        with patch("app.clustering.get_recent_relevant_messages", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [mock_msg]
            result = await is_near_duplicate(MagicMock(), text)
        assert result is True

    @pytest.mark.asyncio
    async def test_different_text_not_duplicate(self):
        text_new = "Large explosion reported in Beirut right now"
        mock_msg = MagicMock()
        mock_msg.raw_text = "Markets close early in Tokyo"

        with patch("app.clustering.get_recent_relevant_messages", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [mock_msg]
            result = await is_near_duplicate(MagicMock(), text_new)
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_text_not_duplicate(self):
        with patch("app.clustering.get_recent_relevant_messages", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            result = await is_near_duplicate(MagicMock(), "")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_recent_messages(self):
        with patch("app.clustering.get_recent_relevant_messages", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            result = await is_near_duplicate(MagicMock(), "Explosion in Beirut")
        assert result is False
