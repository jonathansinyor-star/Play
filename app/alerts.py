"""Alert dispatch module.

Formats and sends EARLY WARNING and CONFIRMATION ALERT notifications to all
configured destinations:
  - Telegram DM (via the same Telethon client)
  - Desktop notification (plyer)
  - Webhook (HTTP POST)
  - Email (aiosmtplib)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from app import config
from app.models import AlertEvent, Incident, Message
from app.scoring import ScoreResult
from app.utils import fmt_timestamp, truncate

log = logging.getLogger(__name__)

# Module-level reference to the Telethon client (set by telegram_client.py)
_telegram_client: Any = None


def set_telegram_client(client: Any) -> None:
    global _telegram_client
    _telegram_client = client


# ─── Formatters ──────────────────────────────────────────────────────────────

def _format_early_warning(
    message: Message,
    incident: Incident,
    score_result: ScoreResult,
    source_name: str,
) -> str:
    lang_labels = {"ar": "Arabic", "he": "Hebrew", "en": "English", "unknown": "Unknown"}
    lang = lang_labels.get(message.detected_language or "unknown", message.detected_language)
    keywords = ", ".join(
        {h.keyword for h in score_result.factors  # type: ignore[attr-defined]
         if hasattr(h, "keyword")} or ["(see breakdown)"]
    )
    # Gather actual keyword hits from classification
    kw_hits = [
        f.reason
        for f in score_result.factors
        if f.name in ("location_keywords", "event_keywords")
    ]

    lines = [
        "🚨 EARLY WARNING: POSSIBLE BEIRUT INCIDENT",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Time        : {fmt_timestamp(message.timestamp)}",
        f"Source      : {source_name}",
        f"Language    : {lang}",
        f"Confidence  : {score_result.total:.0f}/100",
        f"Incident ID : #{incident.id}",
        "",
        "📝 Original text:",
        truncate(message.raw_text or "(no text)", 500),
    ]
    if message.english_translation and message.detected_language != "en":
        lines += [
            "",
            "🔤 English translation:",
            truncate(message.english_translation, 500),
        ]
    lines += [
        "",
        "📊 Score breakdown:",
        score_result.breakdown_text(),
        "",
        f"🔗 Dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}/incidents/{incident.id}",
    ]
    return "\n".join(lines)


def _format_confirmation(
    incident: Incident,
    messages: list[Message],
    score_result: ScoreResult,
    reason: str,
) -> str:
    lines = [
        "✅ CONFIRMED / HIGHER CONFIDENCE BEIRUT INCIDENT",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Incident ID  : #{incident.id}",
        f"Confidence   : {incident.confidence_score:.0f}/100",
        f"First seen   : {fmt_timestamp(incident.first_seen)}",
        f"Last updated : {fmt_timestamp(incident.last_updated)}",
        f"Reports      : {len(messages)} from {len({m.source_id for m in messages})} source(s)",
        f"Location     : {incident.location_hint or 'Beirut'}",
        "",
        f"📈 Reason confidence increased: {reason}",
        "",
        "📋 Supporting reports:",
    ]
    for i, msg in enumerate(messages[:5], 1):
        source = (msg.source.name if msg.source else f"Source #{msg.source_id}")
        lines.append(f"\n  [{i}] {source} — {fmt_timestamp(msg.timestamp)}")
        lines.append(f"  {truncate(msg.raw_text or '', 200)}")
        if msg.english_translation and msg.detected_language != "en":
            lines.append(f"  (EN) {truncate(msg.english_translation, 200)}")
    if len(messages) > 5:
        lines.append(f"\n  … and {len(messages) - 5} more reports.")
    lines += [
        "",
        f"🔗 Dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}/incidents/{incident.id}",
    ]
    return "\n".join(lines)


def _build_payload(
    level: str,
    incident: Incident,
    messages: list[Message],
    score_result: ScoreResult,
    source_name: str,
    reason: str,
) -> dict[str, Any]:
    """Build structured JSON payload for webhook / email."""
    return {
        "alert_level": level,
        "incident_id": incident.id,
        "confidence_score": incident.confidence_score,
        "status": incident.status,
        "first_seen": incident.first_seen.isoformat(),
        "last_updated": incident.last_updated.isoformat(),
        "location_hint": incident.location_hint,
        "reason": reason,
        "source_count": len({m.source_id for m in messages}),
        "message_count": len(messages),
        "messages": [
            {
                "source": (m.source.name if m.source else source_name),
                "timestamp": m.timestamp.isoformat(),
                "language": m.detected_language,
                "raw_text": m.raw_text,
                "english_translation": m.english_translation,
            }
            for m in messages[:10]
        ],
        "dashboard_url": f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}/incidents/{incident.id}",
    }


# ─── Senders ─────────────────────────────────────────────────────────────────

async def _send_telegram_dm(text: str) -> bool:
    if not _telegram_client:
        log.warning("Telegram client not set; skipping DM alert")
        return False
    if not config.ALERT_TELEGRAM_CHAT_ID:
        log.warning("ALERT_TELEGRAM_CHAT_ID not configured; skipping DM alert")
        return False
    try:
        await _telegram_client.send_message(config.ALERT_TELEGRAM_CHAT_ID, text)
        return True
    except Exception as exc:
        log.error("Failed to send Telegram DM: %s", exc)
        return False


async def _send_webhook(payload: dict[str, Any]) -> bool:
    if not config.WEBHOOK_URL:
        return False
    try:
        import httpx
        headers = {"Content-Type": "application/json"}
        if config.WEBHOOK_SECRET:
            headers["Authorization"] = f"Bearer {config.WEBHOOK_SECRET}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(config.WEBHOOK_URL, json=payload, headers=headers)
            resp.raise_for_status()
        return True
    except Exception as exc:
        log.error("Webhook delivery failed: %s", exc)
        return False


def _send_desktop_notification(title: str, body: str) -> bool:
    try:
        from plyer import notification as plyer_notif
        plyer_notif.notify(
            title=title,
            message=truncate(body, 200),
            app_name="Beirut Monitor",
            timeout=15,
        )
        return True
    except Exception as exc:
        log.warning("Desktop notification failed: %s", exc)
        return False


async def _send_email(subject: str, body: str) -> bool:
    if not all([config.SMTP_HOST, config.SMTP_USER, config.ALERT_EMAIL]):
        return False
    try:
        import aiosmtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = config.SMTP_USER
        msg["To"] = config.ALERT_EMAIL
        msg["Subject"] = subject
        msg.set_content(body)
        await aiosmtplib.send(
            msg,
            hostname=config.SMTP_HOST,
            port=config.SMTP_PORT,
            username=config.SMTP_USER,
            password=config.SMTP_PASSWORD,
            start_tls=True,
        )
        return True
    except Exception as exc:
        log.error("Email delivery failed: %s", exc)
        return False


# ─── Public API ──────────────────────────────────────────────────────────────

async def send_early_warning(
    message: Message,
    incident: Incident,
    score_result: ScoreResult,
    source_name: str,
) -> AlertEvent:
    """Dispatch an EARLY WARNING alert to all configured destinations."""
    text = _format_early_warning(message, incident, score_result, source_name)
    payload = _build_payload(
        "early_warning",
        incident,
        [message],
        score_result,
        source_name,
        reason="Single high-scoring message triggered early warning threshold.",
    )

    sent_to: list[str] = []
    errors: list[str] = []

    tasks = [
        ("telegram_dm", _send_telegram_dm(text)),
        ("webhook", _send_webhook(payload)),
        ("email", _send_email("🚨 EARLY WARNING: Beirut Incident", text)),
    ]

    for dest_name, coro in tasks:
        try:
            ok = await coro
            if ok:
                sent_to.append(dest_name)
        except Exception as exc:
            errors.append(f"{dest_name}: {exc}")

    # Desktop notification runs in a thread to avoid blocking
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        _send_desktop_notification,
        "🚨 EARLY WARNING: Beirut",
        truncate(message.english_translation or message.raw_text or "", 150),
    )
    if True:  # always try desktop
        sent_to.append("desktop")

    log.info("EARLY WARNING dispatched to: %s (incident #%d)", sent_to, incident.id)

    return AlertEvent(
        incident_id=incident.id,
        alert_level="early_warning",
        sent_to=sent_to,
        payload=payload,
        success=len(sent_to) > 0,
        error_details="; ".join(errors) or None,
    )


async def send_confirmation_alert(
    incident: Incident,
    messages: list[Message],
    score_result: ScoreResult,
    reason: str,
    source_name: str = "multiple sources",
) -> AlertEvent:
    """Dispatch a CONFIRMATION / higher-confidence alert."""
    text = _format_confirmation(incident, messages, score_result, reason)
    payload = _build_payload(
        "confirmation",
        incident,
        messages,
        score_result,
        source_name,
        reason=reason,
    )

    sent_to: list[str] = []
    errors: list[str] = []

    tasks = [
        ("telegram_dm", _send_telegram_dm(text)),
        ("webhook", _send_webhook(payload)),
        ("email", _send_email("✅ CONFIRMED: Beirut Incident", text)),
    ]

    for dest_name, coro in tasks:
        try:
            ok = await coro
            if ok:
                sent_to.append(dest_name)
        except Exception as exc:
            errors.append(f"{dest_name}: {exc}")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        _send_desktop_notification,
        "✅ CONFIRMED: Beirut Incident",
        f"Confidence: {incident.confidence_score:.0f}/100 — {reason}",
    )
    sent_to.append("desktop")

    log.info(
        "CONFIRMATION ALERT dispatched to: %s (incident #%d, confidence=%.1f)",
        sent_to,
        incident.id,
        incident.confidence_score,
    )

    return AlertEvent(
        incident_id=incident.id,
        alert_level="confirmation",
        sent_to=sent_to,
        payload=payload,
        success=len(sent_to) > 0,
        error_details="; ".join(errors) or None,
    )
