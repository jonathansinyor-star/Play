"""SQLAlchemy ORM models.

Designed for SQLite (MVP) but compatible with PostgreSQL — swap DB_URL in .env.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.sqlite import JSON


class Base(DeclarativeBase):
    pass


class Source(Base):
    """A monitored Telegram channel or group."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), index=True)
    trust_tier: Mapped[int] = mapped_column(Integer, default=3)  # 1=highest, 3=lowest
    language_hint: Mapped[str | None] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    messages: Mapped[list[Message]] = relationship("Message", back_populates="source")


class Message(Base):
    """A raw message ingested from Telegram."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sources.id"))
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    sender: Mapped[str | None] = mapped_column(String(255))
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    raw_text: Mapped[str | None] = mapped_column(Text)
    detected_language: Mapped[str | None] = mapped_column(String(16))
    english_translation: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(64))
    media_metadata: Mapped[Any | None] = mapped_column(JSON)
    urls: Mapped[Any | None] = mapped_column(JSON)
    is_forward: Mapped[bool] = mapped_column(Boolean, default=False)
    forward_from: Mapped[str | None] = mapped_column(String(255))
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    is_relevant: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    source: Mapped[Source | None] = relationship("Source", back_populates="messages")
    keyword_matches: Mapped[list[KeywordMatch]] = relationship(
        "KeywordMatch", back_populates="message", cascade="all, delete-orphan"
    )
    incident_links: Mapped[list[IncidentMessage]] = relationship(
        "IncidentMessage", back_populates="message"
    )


class Translation(Base):
    """Translation record for a message."""

    __tablename__ = "translations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(Integer, ForeignKey("messages.id"), index=True)
    source_language: Mapped[str | None] = mapped_column(String(16))
    target_language: Mapped[str] = mapped_column(String(16), default="en")
    translated_text: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Incident(Base):
    """A clustered incident grouping one or more related messages."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # early_warning | monitoring | confirmed
    status: Mapped[str] = mapped_column(String(32), default="early_warning", index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_updated: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[str | None] = mapped_column(Text)
    location_hint: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    messages: Mapped[list[IncidentMessage]] = relationship(
        "IncidentMessage", back_populates="incident", cascade="all, delete-orphan"
    )
    alerts: Mapped[list[AlertEvent]] = relationship(
        "AlertEvent", back_populates="incident"
    )


class IncidentMessage(Base):
    """Many-to-many join: Incident ↔ Message."""

    __tablename__ = "incident_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("incidents.id"), index=True
    )
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id"), index=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    incident: Mapped[Incident] = relationship("Incident", back_populates="messages")
    message: Mapped[Message] = relationship("Message", back_populates="incident_links")


class AlertEvent(Base):
    """A dispatched alert notification."""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("incidents.id"), index=True
    )
    alert_level: Mapped[str] = mapped_column(String(32))  # early_warning | confirmation
    sent_to: Mapped[Any | None] = mapped_column(JSON)  # list of destination names
    payload: Mapped[Any | None] = mapped_column(JSON)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_details: Mapped[str | None] = mapped_column(Text)

    incident: Mapped[Incident] = relationship("Incident", back_populates="alerts")


class KeywordMatch(Base):
    """Individual keyword hit on a message."""

    __tablename__ = "keyword_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id"), index=True
    )
    keyword: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64))  # location | event | time | speculative | …
    language: Mapped[str | None] = mapped_column(String(16))
    score_contribution: Mapped[float] = mapped_column(Float, default=0.0)

    message: Mapped[Message] = relationship("Message", back_populates="keyword_matches")


class ProcessingLog(Base):
    """Audit log for each pipeline stage."""

    __tablename__ = "processing_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("messages.id"), nullable=True, index=True
    )
    stage: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))  # success | failure | warning
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
