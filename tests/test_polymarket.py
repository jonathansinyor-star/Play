"""Tests for Polymarket market discovery and trade logic."""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from app.polymarket import (
    _is_beirut_market,
    _market_matches_today,
    _extract_yes_token_id,
    place_yes_bet_today,
)
import app.polymarket as pm_module


class TestIsBeirutMarket:
    def test_valid_beirut_market(self):
        market = {"question": "Israel military action against Beirut on April 1, 2026?"}
        assert _is_beirut_market(market) is True

    def test_idf_variant(self):
        market = {"question": "IDF strike in Beirut today?"}
        assert _is_beirut_market(market) is True

    def test_unrelated_market(self):
        market = {"question": "Will Bitcoin reach $100k in 2026?"}
        assert _is_beirut_market(market) is False

    def test_beirut_no_military(self):
        market = {"question": "Will Beirut host the 2028 Olympics?"}
        assert _is_beirut_market(market) is False

    def test_uses_title_field(self):
        market = {"title": "Israel military action against Beirut on April 1?"}
        assert _is_beirut_market(market) is True


class TestMarketMatchesToday:
    def test_matches_today(self):
        today = date.today()
        # Build a title with today's date in the format Polymarket uses
        title = today.strftime("Israel military action against Beirut on %B %-d, %Y?").lower()
        market = {"question": title}
        assert _market_matches_today(market) is True

    def test_does_not_match_wrong_date(self):
        market = {"question": "Israel military action against Beirut on January 1, 2020?"}
        assert _market_matches_today(market) is False

    def test_empty_question(self):
        assert _market_matches_today({}) is False


class TestExtractYesTokenId:
    def test_tokens_list_with_dicts(self):
        market = {
            "tokens": [
                {"outcome": "Yes", "token_id": "abc123"},
                {"outcome": "No", "token_id": "def456"},
            ]
        }
        assert _extract_yes_token_id(market) == "abc123"

    def test_tokens_list_two_strings(self):
        market = {"tokens": ["yes_token_id_here", "no_token_id_here"]}
        assert _extract_yes_token_id(market) == "yes_token_id_here"

    def test_yes_token_id_direct_field(self):
        market = {"yes_token_id": "direct_yes_id"}
        assert _extract_yes_token_id(market) == "direct_yes_id"

    def test_outcomes_array(self):
        market = {
            "outcomes": [
                {"name": "Yes", "id": "outcome_yes_id"},
                {"name": "No", "id": "outcome_no_id"},
            ]
        }
        assert _extract_yes_token_id(market) == "outcome_yes_id"

    def test_no_token_returns_none(self):
        assert _extract_yes_token_id({}) is None


class TestPlaceYesBetToday:
    @pytest.mark.asyncio
    async def test_skips_if_already_traded(self):
        today = date.today().isoformat()
        pm_module._traded_today[today] = True
        try:
            result = await place_yes_bet_today(amount_usdc=100)
            assert result["skipped"] is True
            assert result["success"] is False
        finally:
            pm_module._traded_today.pop(today, None)

    @pytest.mark.asyncio
    async def test_returns_error_if_market_not_found(self):
        pm_module._traded_today.pop(date.today().isoformat(), None)
        with patch("app.polymarket.find_todays_beirut_market", new_callable=AsyncMock, return_value=None):
            result = await place_yes_bet_today(amount_usdc=100)
        assert result["success"] is False
        assert result["error"] == "market_not_found"

    @pytest.mark.asyncio
    async def test_returns_error_if_credentials_missing(self):
        pm_module._traded_today.pop(date.today().isoformat(), None)
        fake_market = {
            "question": "Israel military action against Beirut?",
            "tokens": [{"outcome": "Yes", "token_id": "tok123"}],
        }
        with patch("app.polymarket.find_todays_beirut_market", new_callable=AsyncMock, return_value=fake_market), \
             patch("app.config.POLYMARKET_API_KEY", ""), \
             patch("app.config.POLYMARKET_PRIVATE_KEY", ""):
            result = await place_yes_bet_today(amount_usdc=100)
        assert result["success"] is False
        assert result["error"] == "credentials_missing"
