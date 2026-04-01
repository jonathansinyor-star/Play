"""Shared utilities: logging setup, async helpers, text helpers."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with a readable format."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s – %(message)s"
    logging.basicConfig(
        level=numeric,
        format=fmt,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Silence noisy third-party loggers
    for noisy in ("telethon", "aiosqlite", "sqlalchemy.engine", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from text."""
    pattern = r"https?://[^\s\)\]\>\"']+"
    return re.findall(pattern, text)


def truncate(text: str, max_len: int = 300) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def retry_async(
    coro_fn,
    *args,
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "task",
    **kwargs,
) -> Any:
    """Retry an async coroutine with exponential back-off."""
    log = logging.getLogger(__name__)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1))
                log.warning("%s attempt %d/%d failed: %s – retrying in %.1fs", label, attempt, attempts, exc, delay)
                await asyncio.sleep(delay)
    log.error("%s failed after %d attempts: %s", label, attempts, last_exc)
    raise last_exc  # type: ignore[misc]


def fmt_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def is_question_only(text: str) -> bool:
    """Return True if the message appears to be only a question with no assertions."""
    stripped = text.strip()
    # Simple heuristic: ends with ? and has no other sentence-ending punctuation
    sentences = re.split(r"[.!؟。]", stripped)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return False
    all_questions = all(
        s.endswith("?") or "؟" in s
        for s in sentences
    )
    return all_questions


def detect_media_type(message_obj: Any) -> str | None:
    """Given a Telethon Message object, return a human-readable media type string."""
    if message_obj is None:
        return None
    media = getattr(message_obj, "media", None)
    if media is None:
        return None
    class_name = type(media).__name__
    mapping = {
        "MessageMediaPhoto": "photo",
        "MessageMediaDocument": "document",
        "MessageMediaGeo": "location",
        "MessageMediaPoll": "poll",
        "MessageMediaWebPage": "webpage",
        "MessageMediaContact": "contact",
        "MessageMediaVenue": "venue",
        "MessageMediaDice": "dice",
    }
    return mapping.get(class_name, class_name.replace("MessageMedia", "").lower() or "media")


def get_media_metadata(message_obj: Any) -> dict[str, Any] | None:
    """Extract lightweight media metadata from a Telethon Message."""
    media = getattr(message_obj, "media", None)
    if media is None:
        return None
    meta: dict[str, Any] = {"type": detect_media_type(message_obj)}
    # For documents (video, voice, etc.) grab mime type and size
    doc = getattr(media, "document", None)
    if doc:
        meta["mime_type"] = getattr(doc, "mime_type", None)
        meta["size"] = getattr(doc, "size", None)
    return meta
