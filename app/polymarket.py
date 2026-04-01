"""Polymarket CLOB integration.

Finds today's 'Israel military action against Beirut' daily market and
places a fixed-amount market buy on the YES outcome.

Requires:
  pip install py-clob-client

Credentials go in .env (see .env.example).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

import httpx

from app import config

log = logging.getLogger(__name__)

# Gamma Markets API – public, no auth needed for reads
_GAMMA_API = "https://gamma-api.polymarket.com"
# CLOB API – needs credentials for writes
_CLOB_HOST = "https://clob.polymarket.com"

# Search terms that identify this market series
_MARKET_SEARCH_TERMS = ["beirut", "israel", "military action"]


# ─── Market discovery ────────────────────────────────────────────────────────

async def _fetch_markets(search: str) -> list[dict]:
    """Query the Gamma API for markets matching *search*."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_GAMMA_API}/markets",
            params={"search": search, "active": "true", "closed": "false"},
        )
        resp.raise_for_status()
        data = resp.json()
        # Gamma returns either a list or {"markets": [...]}
        if isinstance(data, list):
            return data
        return data.get("markets", [])


def _market_matches_today(market: dict) -> bool:
    """Return True if the market's question refers to today's date."""
    today = date.today()
    question = (market.get("question") or market.get("title") or "").lower()

    # Check all common date formats that Polymarket uses in market titles
    date_variants = [
        today.strftime("%B %d, %Y").lower(),       # April 01, 2026
        today.strftime("%B %-d, %Y").lower(),       # April 1, 2026  (Linux)
        today.strftime("%B %#d, %Y").lower(),       # April 1, 2026  (Windows)
        today.strftime("%b %d, %Y").lower(),        # Apr 01, 2026
        today.strftime("%Y-%m-%d").lower(),         # 2026-04-01
        today.strftime("%-m/%-d/%Y").lower(),       # 4/1/2026
    ]
    return any(d in question for d in date_variants)


def _is_beirut_market(market: dict) -> bool:
    question = (market.get("question") or market.get("title") or "").lower()
    return (
        "beirut" in question
        and any(w in question for w in ("israel", "military", "idf", "strike", "action"))
    )


async def find_todays_beirut_market() -> dict | None:
    """Search Polymarket for today's Israel/Beirut daily market.

    Returns the full market dict or None if not found.
    """
    try:
        markets = await _fetch_markets("Israel military action Beirut")
        log.debug("Gamma API returned %d markets for Beirut search", len(markets))

        for market in markets:
            if _is_beirut_market(market) and _market_matches_today(market):
                log.info(
                    "Found today's Beirut market: '%s' (conditionId=%s)",
                    market.get("question") or market.get("title"),
                    market.get("conditionId") or market.get("id"),
                )
                return market

        # Fallback: try broader search
        markets2 = await _fetch_markets("Beirut")
        for market in markets2:
            if _is_beirut_market(market) and _market_matches_today(market):
                return market

        log.warning("No today's Beirut market found on Polymarket")
        return None

    except Exception as exc:
        log.error("Failed to fetch Polymarket markets: %s", exc)
        return None


def _extract_yes_token_id(market: dict) -> str | None:
    """Extract the YES outcome token_id from a market dict."""
    # Gamma API nests outcomes/tokens differently from CLOB API
    tokens = market.get("tokens") or market.get("clobTokenIds") or []
    if isinstance(tokens, list):
        for token in tokens:
            if isinstance(token, dict):
                outcome = (token.get("outcome") or "").lower()
                if outcome == "yes":
                    return str(token.get("token_id") or token.get("id") or "")
            elif isinstance(token, str) and len(tokens) == 2:
                # Some responses are just [yes_id, no_id]
                return tokens[0]

    # Direct field
    yes_id = market.get("yes_token_id") or market.get("yesTokenId")
    if yes_id:
        return str(yes_id)

    # outcomes array
    outcomes = market.get("outcomes") or []
    for outcome in outcomes:
        if isinstance(outcome, dict) and outcome.get("name", "").lower() == "yes":
            return str(outcome.get("id") or outcome.get("token_id") or "")

    log.warning("Could not extract YES token_id from market: %s", list(market.keys()))
    return None


# ─── Order placement ─────────────────────────────────────────────────────────

def _build_clob_client():
    """Build and return a py-clob-client ClobClient instance."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    creds = ApiCreds(
        api_key=config.POLYMARKET_API_KEY,
        api_secret=config.POLYMARKET_API_SECRET,
        api_passphrase=config.POLYMARKET_API_PASSPHRASE,
    )
    return ClobClient(
        host=_CLOB_HOST,
        key=config.POLYMARKET_PRIVATE_KEY,
        chain_id=137,  # Polygon mainnet
        creds=creds,
        signature_type=0,  # EOA signature
    )


async def _place_market_buy(token_id: str, amount_usdc: float) -> dict:
    """Place a market buy order on the CLOB. Runs in a thread (sync client)."""
    def _sync_place():
        from py_clob_client.clob_types import MarketOrderArgs
        from py_clob_client.constants import ZERO_ADDRESS

        client = _build_clob_client()

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usdc,   # USDC amount (client handles decimals)
        )
        signed_order = client.create_market_order(order_args)
        # FOK = Fill-Or-Kill: best for market orders
        from py_clob_client.clob_types import OrderType
        result = client.post_order(signed_order, OrderType.FOK)
        return result

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_place)


# ─── Main public function ─────────────────────────────────────────────────────

# Guard: only trade once per day (date → True/False)
_traded_today: dict[str, bool] = {}


async def place_yes_bet_today(
    amount_usdc: float | None = None,
    trigger_reason: str = "early_warning",
) -> dict[str, Any]:
    """Find today's Beirut market and place a YES market buy.

    Returns a result dict with keys: success, market_title, token_id,
    amount, order_response, error.
    """
    today_key = date.today().isoformat()
    if _traded_today.get(today_key):
        log.info("Already traded today's Beirut market (%s) – skipping", today_key)
        return {"success": False, "error": "already_traded_today", "skipped": True}

    amount = amount_usdc or config.POLYMARKET_TRADE_AMOUNT
    result: dict[str, Any] = {
        "success": False,
        "market_title": None,
        "token_id": None,
        "amount": amount,
        "order_response": None,
        "error": None,
        "trigger_reason": trigger_reason,
        "timestamp": datetime.utcnow().isoformat(),
    }

    # 1. Find today's market
    market = await find_todays_beirut_market()
    if not market:
        result["error"] = "market_not_found"
        log.warning("Cannot place bet – today's Beirut market not found")
        return result

    result["market_title"] = market.get("question") or market.get("title")
    result["market_id"] = market.get("conditionId") or market.get("id")

    # 2. Extract YES token
    token_id = _extract_yes_token_id(market)
    if not token_id:
        result["error"] = "yes_token_not_found"
        log.error("Cannot place bet – YES token_id not found in market")
        return result

    result["token_id"] = token_id

    # 3. Check credentials are configured
    if not all([
        config.POLYMARKET_API_KEY,
        config.POLYMARKET_API_SECRET,
        config.POLYMARKET_API_PASSPHRASE,
        config.POLYMARKET_PRIVATE_KEY,
    ]):
        result["error"] = "credentials_missing"
        log.error("Polymarket credentials not configured in .env")
        return result

    # 4. Place the order
    try:
        log.info(
            "Placing Polymarket YES bet: market='%s' token=%s amount=$%.2f reason=%s",
            result["market_title"],
            token_id,
            amount,
            trigger_reason,
        )
        order_resp = await _place_market_buy(token_id, amount)
        result["order_response"] = order_resp
        result["success"] = True
        _traded_today[today_key] = True
        log.info("Polymarket order placed successfully: %s", order_resp)

    except Exception as exc:
        result["error"] = str(exc)
        log.error("Polymarket order failed: %s", exc)

    return result


async def get_current_yes_price() -> float | None:
    """Return the current YES price (0–1) for today's Beirut market, or None."""
    try:
        market = await find_todays_beirut_market()
        if not market:
            return None
        token_id = _extract_yes_token_id(market)
        if not token_id:
            return None

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_CLOB_HOST}/price",
                params={"token_id": token_id, "side": "BUY"},
            )
            resp.raise_for_status()
            data = resp.json()
            price = float(data.get("price", 0))
            return price
    except Exception as exc:
        log.warning("Could not fetch YES price: %s", exc)
        return None
