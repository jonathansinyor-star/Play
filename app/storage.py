"""Async database layer using SQLAlchemy + aiosqlite.

All public functions accept an AsyncSession and return ORM objects.
The session is managed by the caller (ingest pipeline, dashboard, etc.).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import AsyncIterator

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from app import config
from app.models import (
    AlertEvent,
    Base,
    Incident,
    IncidentMessage,
    KeywordMatch,
    Message,
    ProcessingLog,
    Source,
    Translation,
)

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            config.DB_URL,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False} if "sqlite" in config.DB_URL else {},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            yield session


async def init_db() -> None:
    """Create all tables if they don't exist."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database initialised at %s", config.DB_URL)


# ─── Source CRUD ─────────────────────────────────────────────────────────────

async def upsert_source(session: AsyncSession, src: "config.SourceConfig") -> Source:
    """Insert or update a source row from a SourceConfig."""
    identifier = src.chat_id or src.username
    stmt = select(Source).where(
        (Source.chat_id == identifier) | (Source.username == src.username)
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        existing.name = src.name
        existing.trust_tier = src.trust_tier
        existing.language_hint = src.language
        existing.enabled = src.enabled
        if src.chat_id:
            existing.chat_id = src.chat_id
        return existing
    new_src = Source(
        name=src.name,
        username=src.username,
        chat_id=src.chat_id or src.username,
        trust_tier=src.trust_tier,
        language_hint=src.language,
        enabled=src.enabled,
    )
    session.add(new_src)
    await session.flush()
    return new_src


async def get_source_by_chat_id(session: AsyncSession, chat_id: str) -> Source | None:
    result = await session.execute(
        select(Source).where(Source.chat_id == chat_id)
    )
    return result.scalar_one_or_none()


async def get_all_enabled_sources(session: AsyncSession) -> list[Source]:
    result = await session.execute(
        select(Source).where(Source.enabled == True)  # noqa: E712
    )
    return list(result.scalars().all())


# ─── Message CRUD ────────────────────────────────────────────────────────────

async def save_message(session: AsyncSession, msg: Message) -> Message:
    session.add(msg)
    await session.flush()
    return msg


async def get_message(session: AsyncSession, message_id: int) -> Message | None:
    result = await session.execute(
        select(Message)
        .options(selectinload(Message.keyword_matches))
        .where(Message.id == message_id)
    )
    return result.scalar_one_or_none()


async def message_exists(
    session: AsyncSession, chat_id: str, message_id: int
) -> bool:
    result = await session.execute(
        select(Message.id).where(
            Message.chat_id == chat_id, Message.message_id == message_id
        )
    )
    return result.scalar_one_or_none() is not None


async def update_message_scores(
    session: AsyncSession,
    message_id: int,
    score: float,
    is_relevant: bool,
    language: str | None,
    translation: str | None,
    processed_at: datetime,
) -> None:
    await session.execute(
        update(Message)
        .where(Message.id == message_id)
        .values(
            relevance_score=score,
            is_relevant=is_relevant,
            detected_language=language,
            english_translation=translation,
            processed_at=processed_at,
        )
    )


# ─── Translation CRUD ────────────────────────────────────────────────────────

async def save_translation(session: AsyncSession, t: Translation) -> Translation:
    session.add(t)
    await session.flush()
    return t


# ─── Incident CRUD ───────────────────────────────────────────────────────────

async def create_incident(session: AsyncSession, incident: Incident) -> Incident:
    session.add(incident)
    await session.flush()
    return incident


async def get_incident(session: AsyncSession, incident_id: int) -> Incident | None:
    result = await session.execute(
        select(Incident)
        .options(
            selectinload(Incident.messages).selectinload(IncidentMessage.message)
        )
        .where(Incident.id == incident_id)
    )
    return result.scalar_one_or_none()


async def get_recent_incidents(
    session: AsyncSession,
    window_seconds: int | None = None,
    limit: int = 50,
) -> list[Incident]:
    """Return active/recent incidents, optionally within a time window."""
    stmt = select(Incident).order_by(Incident.last_updated.desc()).limit(limit)
    if window_seconds:
        cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
        stmt = stmt.where(Incident.last_updated >= cutoff)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_incident(
    session: AsyncSession,
    incident_id: int,
    **kwargs,
) -> None:
    await session.execute(
        update(Incident).where(Incident.id == incident_id).values(**kwargs)
    )


async def add_message_to_incident(
    session: AsyncSession, incident_id: int, message_id: int
) -> None:
    # Avoid duplicates
    existing = await session.execute(
        select(IncidentMessage).where(
            IncidentMessage.incident_id == incident_id,
            IncidentMessage.message_id == message_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        session.add(
            IncidentMessage(incident_id=incident_id, message_id=message_id)
        )
        await session.flush()


async def get_recent_relevant_messages(
    session: AsyncSession,
    window_seconds: int,
    min_score: float = 0.0,
) -> list[Message]:
    """Fetch scored messages from the last N seconds."""
    cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
    result = await session.execute(
        select(Message)
        .options(selectinload(Message.source))
        .where(
            Message.timestamp >= cutoff,
            Message.is_relevant == True,  # noqa: E712
            Message.relevance_score >= min_score,
        )
        .order_by(Message.timestamp.desc())
    )
    return list(result.scalars().all())


# ─── Keyword matches ─────────────────────────────────────────────────────────

async def save_keyword_matches(
    session: AsyncSession, matches: list[KeywordMatch]
) -> None:
    for km in matches:
        session.add(km)
    await session.flush()


# ─── Alert events ────────────────────────────────────────────────────────────

async def save_alert_event(session: AsyncSession, alert: AlertEvent) -> AlertEvent:
    session.add(alert)
    await session.flush()
    return alert


async def get_alerts_for_incident(
    session: AsyncSession, incident_id: int
) -> list[AlertEvent]:
    result = await session.execute(
        select(AlertEvent)
        .where(AlertEvent.incident_id == incident_id)
        .order_by(AlertEvent.sent_at.asc())
    )
    return list(result.scalars().all())


# ─── Processing log ──────────────────────────────────────────────────────────

async def log_pipeline_event(
    session: AsyncSession,
    stage: str,
    status: str,
    details: str | None = None,
    message_id: int | None = None,
) -> None:
    session.add(
        ProcessingLog(
            message_id=message_id,
            stage=stage,
            status=status,
            details=details,
        )
    )
    await session.flush()


# ─── Dashboard queries ───────────────────────────────────────────────────────

async def get_recent_messages(
    session: AsyncSession,
    limit: int = 100,
    lang_filter: str | None = None,
    source_id: int | None = None,
    min_score: float | None = None,
) -> list[Message]:
    stmt = (
        select(Message)
        .options(selectinload(Message.source))
        .order_by(Message.timestamp.desc())
        .limit(limit)
    )
    if lang_filter:
        stmt = stmt.where(Message.detected_language == lang_filter)
    if source_id is not None:
        stmt = stmt.where(Message.source_id == source_id)
    if min_score is not None:
        stmt = stmt.where(Message.relevance_score >= min_score)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_source_stats(session: AsyncSession) -> list[dict]:
    """Return per-source message counts and average relevance."""
    result = await session.execute(
        select(
            Source.id,
            Source.name,
            Source.trust_tier,
            func.count(Message.id).label("message_count"),
            func.avg(Message.relevance_score).label("avg_score"),
        )
        .outerjoin(Message, Message.source_id == Source.id)
        .group_by(Source.id)
        .order_by(Source.trust_tier.asc())
    )
    rows = result.all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "trust_tier": r.trust_tier,
            "message_count": r.message_count or 0,
            "avg_score": round(r.avg_score or 0.0, 1),
        }
        for r in rows
    ]
