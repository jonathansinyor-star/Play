"""Incident clustering and deduplication.

Groups related messages into Incident records.
Two messages are considered part of the same incident if:
  - Both have a location match in the same area
  - Both have an event match
  - Their timestamps are within INCIDENT_CLUSTER_WINDOW_SECONDS

The module also handles deduplication (near-identical text from multiple sources).
"""

from __future__ import annotations

import difflib
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.models import Incident, Message
from app.scoring import score_incident
from app.storage import (
    add_message_to_incident,
    create_incident,
    get_recent_incidents,
    get_recent_relevant_messages,
    update_incident,
)
from app.utils import utcnow

log = logging.getLogger(__name__)


async def is_near_duplicate(
    session: AsyncSession, new_text: str, window_seconds: int = 300
) -> bool:
    """Return True if a nearly identical message was seen in the last N seconds."""
    if not new_text:
        return False
    recent = await get_recent_relevant_messages(
        session,
        window_seconds=window_seconds,
        min_score=0.0,
    )
    norm_new = new_text.lower().strip()
    for msg in recent:
        if not msg.raw_text:
            continue
        norm_old = msg.raw_text.lower().strip()
        ratio = difflib.SequenceMatcher(None, norm_new, norm_old).ratio()
        if ratio > 0.85:
            log.debug("Near-duplicate detected (ratio=%.2f)", ratio)
            return True
    return False


async def find_or_create_incident(
    session: AsyncSession,
    message: Message,
    classification_has_location: bool,
    classification_has_event: bool,
) -> Incident | None:
    """Find an active incident to attach this message to, or create a new one.

    Returns the incident, or None if the message is not relevant.
    """
    if not (classification_has_location and classification_has_event):
        return None

    now = utcnow()
    candidates = await get_recent_incidents(
        session,
        window_seconds=config.INCIDENT_CLUSTER_WINDOW_SECONDS,
    )

    # Try to match an existing active incident
    for incident in candidates:
        if incident.status in ("early_warning", "monitoring"):
            # Attach message to this incident
            await add_message_to_incident(session, incident.id, message.id)
            await _refresh_incident(session, incident, now)
            log.info(
                "Message %d attached to existing incident %d (status=%s)",
                message.id,
                incident.id,
                incident.status,
            )
            return incident

    # No matching incident – create a new one
    incident = Incident(
        status="early_warning",
        first_seen=message.timestamp,
        last_updated=now,
        confidence_score=message.relevance_score,
        location_hint=_guess_location(message),
    )
    incident = await create_incident(session, incident)
    await add_message_to_incident(session, incident.id, message.id)
    log.info(
        "New incident %d created for message %d (score=%.1f)",
        incident.id,
        message.id,
        message.relevance_score,
    )
    return incident


async def _refresh_incident(
    session: AsyncSession, incident: Incident, now: datetime
) -> None:
    """Recalculate incident confidence from all linked messages."""
    from sqlalchemy import select
    from app.models import IncidentMessage

    result = await session.execute(
        select(Message)
        .join(IncidentMessage, IncidentMessage.message_id == Message.id)
        .where(IncidentMessage.incident_id == incident.id)
    )
    linked_messages: list[Message] = list(result.scalars().all())

    scores = [m.relevance_score for m in linked_messages if m.relevance_score]
    source_ids = {m.source_id for m in linked_messages if m.source_id}

    new_confidence = score_incident(scores, len(source_ids))
    new_status = _determine_status(new_confidence, len(linked_messages))

    await update_incident(
        session,
        incident.id,
        confidence_score=new_confidence,
        status=new_status,
        last_updated=now,
    )
    incident.confidence_score = new_confidence
    incident.status = new_status
    incident.last_updated = now


def _determine_status(confidence: float, message_count: int) -> str:
    if confidence >= config.CONFIRMATION_THRESHOLD or message_count >= 3:
        return "confirmed"
    if message_count >= 2:
        return "monitoring"
    return "early_warning"


def _guess_location(message: Message) -> str:
    """Simple heuristic: return 'Beirut' or sub-area based on text."""
    text = (message.raw_text or "") + " " + (message.english_translation or "")
    text_lower = text.lower()
    if any(w in text_lower for w in ("dahieh", "dahiyeh", "dahye", "الضاحية", "הדאחיה")):
        return "Dahieh (Southern Beirut Suburbs)"
    if "south" in text_lower or "جنوب" in text_lower or "דרום" in text_lower:
        return "Southern Beirut"
    return "Beirut"
