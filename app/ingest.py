"""Message ingestion pipeline.

Consumes raw message dicts from the asyncio.Queue produced by
TelegramMonitorClient and runs them through:

  1. Deduplication check
  2. Persist raw message
  3. Normalize text
  4. Detect language
  5. Translate to English (non-blocking: alerts fire even if translation lags)
  6. Keyword classification
  7. Scoring
  8. Incident clustering
  9. Dispatch alerts (early warning + confirmation)
 10. Persist keyword matches, translation, alert events

All DB writes happen within a single session per message to minimise latency.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Awaitable

from app import config
from app.alerts import send_confirmation_alert, send_early_warning
from app.classifier import classify
from app.clustering import find_or_create_incident
from app.language import detect_language, needs_translation
from app.models import AlertEvent, Incident, KeywordMatch, Message, Translation
from app.normalize import clean_for_langdetect, normalize
from app.scoring import score_message
from app.storage import (
    get_source_by_chat_id,
    log_pipeline_event,
    message_exists,
    save_alert_event,
    save_keyword_matches,
    save_message,
    save_translation,
    session_scope,
    update_message_scores,
)
from app.translate import translate_to_english
from app.utils import extract_urls, utcnow

log = logging.getLogger(__name__)

# Optional broadcast callback so the dashboard can push live events
_broadcast_fn: Callable[[dict], Awaitable[None]] | None = None


def set_broadcast_fn(fn: Callable[[dict], Awaitable[None]]) -> None:
    global _broadcast_fn
    _broadcast_fn = fn


async def run_pipeline(queue: asyncio.Queue) -> None:
    """Consume the queue forever, processing one message at a time."""
    log.info("Ingest pipeline started")
    while True:
        raw: dict[str, Any] = await queue.get()
        try:
            await _process_message(raw)
        except Exception as exc:
            log.exception("Pipeline error for message %s: %s", raw.get("message_id"), exc)
        finally:
            queue.task_done()


async def _process_message(raw: dict[str, Any]) -> None:
    chat_id: str = str(raw["chat_id"])
    message_id: int = int(raw["message_id"])
    raw_text: str = raw.get("raw_text") or ""
    timestamp: datetime = raw["timestamp"]
    if hasattr(timestamp, "replace"):
        # Strip timezone info for SQLite compatibility
        timestamp = timestamp.replace(tzinfo=None)

    # ── 1. Deduplication (cheap DB check) ────────────────────────────────────
    async with session_scope() as session:
        if await message_exists(session, chat_id, message_id):
            log.debug("Skipping duplicate message %d from chat %s", message_id, chat_id)
            return

        # ── 2. Resolve source ─────────────────────────────────────────────────
        source = await get_source_by_chat_id(session, chat_id)
        source_id = source.id if source else None
        source_name = source.name if source else f"Unknown ({chat_id})"
        trust_tier = source.trust_tier if source else 3

        # ── 3. Persist raw message ────────────────────────────────────────────
        msg = Message(
            source_id=source_id,
            chat_id=chat_id,
            message_id=message_id,
            sender=raw.get("sender"),
            timestamp=timestamp,
            raw_text=raw_text,
            media_type=raw.get("media_type"),
            media_metadata=raw.get("media_metadata"),
            urls=raw.get("urls"),
            is_forward=bool(raw.get("is_forward", False)),
            forward_from=raw.get("forward_from"),
        )
        msg = await save_message(session, msg)
        db_msg_id = msg.id
        await log_pipeline_event(session, "ingest", "success", f"chat={chat_id} msg={message_id}", db_msg_id)

    # ── 4. Language detection ─────────────────────────────────────────────────
    clean_text = clean_for_langdetect(raw_text)
    detected_lang = detect_language(clean_text) if clean_text.strip() else "unknown"
    log.debug("Message %d: detected language=%s", db_msg_id, detected_lang)

    # ── 5. Translation (async, non-blocking for alert path) ───────────────────
    english_translation: str | None = None
    translation_provider: str | None = None
    translation_error: str | None = None

    if needs_translation(detected_lang) and raw_text.strip():
        try:
            english_translation, translation_provider = await translate_to_english(
                raw_text, detected_lang
            )
        except Exception as exc:
            translation_error = str(exc)
            log.warning("Translation failed for message %d: %s", db_msg_id, exc)

    # ── 6. Keyword classification ─────────────────────────────────────────────
    classification = classify(
        original_text=raw_text,
        english_translation=english_translation,
        has_media=bool(raw.get("media_type")),
        is_forward=bool(raw.get("is_forward", False)),
    )

    # ── 7. Scoring ────────────────────────────────────────────────────────────
    score_result = score_message(
        classification=classification,
        trust_tier=trust_tier,
        has_media=bool(raw.get("media_type")),
        is_forward=bool(raw.get("is_forward", False)),
    )
    is_relevant = score_result.total > 0 and (
        classification.has_location_match or classification.has_event_match
    )

    log.info(
        "Message %d score=%.1f relevant=%s lang=%s source='%s'",
        db_msg_id,
        score_result.total,
        is_relevant,
        detected_lang,
        source_name,
    )

    now = utcnow()

    async with session_scope() as session:
        # Update message with scores, language, translation
        await update_message_scores(
            session,
            db_msg_id,
            score=score_result.total,
            is_relevant=is_relevant,
            language=detected_lang,
            translation=english_translation,
            processed_at=now,
        )

        # Persist translation record
        if needs_translation(detected_lang):
            await save_translation(
                session,
                Translation(
                    message_id=db_msg_id,
                    source_language=detected_lang,
                    target_language="en",
                    translated_text=english_translation,
                    provider=translation_provider,
                    success=english_translation is not None,
                    error_message=translation_error,
                ),
            )

        # Persist keyword matches
        kw_matches: list[KeywordMatch] = []
        for hit in classification.hits:
            kw_matches.append(
                KeywordMatch(
                    message_id=db_msg_id,
                    keyword=hit.keyword,
                    category=hit.category,
                    language=hit.language,
                    score_contribution=hit.score,
                )
            )
        if kw_matches:
            await save_keyword_matches(session, kw_matches)

        # ── 8. Incident clustering ────────────────────────────────────────────
        incident: Incident | None = None
        if is_relevant and score_result.is_early_warning:
            # Fetch the updated message object for clustering
            from app.models import Message as MsgModel
            from sqlalchemy import select
            result = await session.execute(
                select(MsgModel).where(MsgModel.id == db_msg_id)
            )
            db_msg = result.scalar_one_or_none()
            if db_msg:
                db_msg.relevance_score = score_result.total
                incident = await find_or_create_incident(
                    session,
                    db_msg,
                    classification_has_location=classification.has_location_match,
                    classification_has_event=classification.has_event_match,
                )

        # ── 9. Early warning alert ────────────────────────────────────────────
        if incident and score_result.is_early_warning:
            # Only send early warning if this incident hasn't had one yet
            from app.storage import get_alerts_for_incident
            prior_alerts = await get_alerts_for_incident(session, incident.id)
            prior_levels = {a.alert_level for a in prior_alerts}

            if "early_warning" not in prior_levels:
                # Reconstruct message object with translation for formatting
                from app.models import Message as MsgModel
                from sqlalchemy import select
                r = await session.execute(select(MsgModel).where(MsgModel.id == db_msg_id))
                db_msg_full = r.scalar_one_or_none()
                if db_msg_full:
                    db_msg_full.english_translation = english_translation
                    db_msg_full.detected_language = detected_lang
                    alert_evt = await send_early_warning(
                        message=db_msg_full,
                        incident=incident,
                        score_result=score_result,
                        source_name=source_name,
                    )
                    await save_alert_event(session, alert_evt)
                    await log_pipeline_event(
                        session, "early_warning", "success",
                        f"incident={incident.id} score={score_result.total}",
                        db_msg_id,
                    )

        # ── 10. Confirmation alert ────────────────────────────────────────────
        if incident and score_result.is_confirmation:
            prior_alerts = await get_alerts_for_incident(session, incident.id)
            prior_levels = {a.alert_level for a in prior_alerts}
            if "confirmation" not in prior_levels:
                # Collect all messages for this incident
                from app.storage import get_incident
                full_incident = await get_incident(session, incident.id)
                msgs_for_alert: list[Message] = []
                if full_incident:
                    for link in full_incident.messages:
                        if link.message:
                            msgs_for_alert.append(link.message)
                reason = _build_confirmation_reason(incident, msgs_for_alert, score_result)
                alert_evt = await send_confirmation_alert(
                    incident=incident,
                    messages=msgs_for_alert or [db_msg_full],
                    score_result=score_result,
                    reason=reason,
                    source_name=source_name,
                )
                await save_alert_event(session, alert_evt)
                await log_pipeline_event(
                    session, "confirmation_alert", "success",
                    f"incident={incident.id} confidence={incident.confidence_score}",
                    db_msg_id,
                )

    # ── 11. Broadcast to dashboard ────────────────────────────────────────────
    if _broadcast_fn:
        try:
            event_data = {
                "type": "new_message",
                "message_id": db_msg_id,
                "chat_id": chat_id,
                "source_name": source_name,
                "timestamp": timestamp.isoformat(),
                "raw_text": raw_text[:300],
                "english_translation": (english_translation or "")[:300],
                "detected_language": detected_lang,
                "relevance_score": round(score_result.total, 1),
                "is_relevant": is_relevant,
                "has_media": bool(raw.get("media_type")),
                "incident_id": incident.id if incident else None,
                "alert_level": (
                    "confirmation" if score_result.is_confirmation
                    else "early_warning" if score_result.is_early_warning
                    else None
                ),
            }
            await _broadcast_fn(event_data)
        except Exception as exc:
            log.warning("Dashboard broadcast failed: %s", exc)


def _build_confirmation_reason(
    incident: Incident,
    messages: list[Message],
    score_result: Any,
) -> str:
    """Generate a human-readable reason string for the confirmation alert."""
    reasons: list[str] = []
    unique_sources = len({m.source_id for m in messages})
    if unique_sources >= 2:
        reasons.append(f"{unique_sources} independent sources reporting")
    if any(m.media_type for m in messages):
        reasons.append("media evidence present")
    if score_result.total >= config.CONFIRMATION_THRESHOLD:
        reasons.append(f"combined score {score_result.total:.0f} exceeds confirmation threshold")
    return "; ".join(reasons) or "high confidence score reached"
