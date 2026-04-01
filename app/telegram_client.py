"""Telegram ingestion client using Telethon.

Connects as a Telegram user account (not a bot) so it can read channels
and groups that bots cannot access.

Usage:
  client = TelegramMonitorClient(queue)
  await client.start()
  await client.join_sources(sources)
  # client now forwards new messages onto the queue
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, MessageMediaPhoto, MessageMediaDocument

from app import config
from app.utils import detect_media_type, get_media_metadata, utcnow

log = logging.getLogger(__name__)


class TelegramMonitorClient:
    """Wraps Telethon to listen on configured channels and forward raw
    message dicts onto an asyncio.Queue for pipeline processing."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue
        self._client: TelegramClient | None = None
        # Maps chat_id (str) → source config identifier
        self._watched_chat_ids: set[str] = set()

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise RuntimeError("Client not started – call start() first")
        return self._client

    async def start(self) -> None:
        """Authenticate and connect the Telethon client."""
        Path(config.TELEGRAM_SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)
        self._client = TelegramClient(
            config.TELEGRAM_SESSION_PATH,
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
        )
        await self._client.start(phone=config.TELEGRAM_PHONE)
        log.info("Telegram client connected as %s", await self._client.get_me())

        @self._client.on(events.NewMessage)
        async def _on_new_message(event: events.NewMessage.Event) -> None:
            await self._handle_event(event)

        log.info("NewMessage handler registered")

    async def stop(self) -> None:
        if self._client:
            await self._client.disconnect()
            log.info("Telegram client disconnected")

    async def send_message(self, chat_id: int | str, text: str) -> None:
        """Send a message (used for alert DMs)."""
        await self.client.send_message(chat_id, text)

    async def join_sources(
        self, sources: list["config.SourceConfig"]
    ) -> dict[str, str]:
        """Resolve usernames to chat IDs and register them as watched.

        Returns a dict mapping display_name → resolved_chat_id.
        """
        resolved: dict[str, str] = {}
        for src in sources:
            if not src.enabled:
                continue
            try:
                chat_id_str = await self._resolve_source(src)
                if chat_id_str:
                    self._watched_chat_ids.add(chat_id_str)
                    resolved[src.name] = chat_id_str
                    log.info("Watching source '%s' → chat_id=%s", src.name, chat_id_str)
            except Exception as exc:
                log.warning("Could not resolve source '%s': %s", src.name, exc)
        return resolved

    async def _resolve_source(self, src: "config.SourceConfig") -> str | None:
        """Return numeric chat_id string for a source."""
        if src.chat_id:
            return str(src.chat_id)
        if src.username:
            try:
                entity = await self.client.get_entity(src.username)
                cid = str(getattr(entity, "id", None))
                return cid
            except Exception as exc:
                log.warning("Failed to get entity for @%s: %s", src.username, exc)
                return None
        return None

    async def _handle_event(self, event: events.NewMessage.Event) -> None:
        """Convert a Telethon event to a plain dict and enqueue it."""
        try:
            msg = event.message
            chat_id = str(event.chat_id)

            # Only process messages from watched channels
            if chat_id not in self._watched_chat_ids:
                return

            # Extract sender
            sender_name: str | None = None
            try:
                sender = await event.get_sender()
                if sender:
                    if hasattr(sender, "username") and sender.username:
                        sender_name = f"@{sender.username}"
                    elif hasattr(sender, "first_name"):
                        parts = [sender.first_name or "", getattr(sender, "last_name", "") or ""]
                        sender_name = " ".join(p for p in parts if p).strip() or None
            except Exception:
                pass

            # Extract forward info
            is_forward = msg.forward is not None
            forward_from: str | None = None
            if is_forward and msg.forward:
                fwd = msg.forward
                if hasattr(fwd, "sender") and fwd.sender:
                    forward_from = getattr(fwd.sender, "username", None)
                if not forward_from and hasattr(fwd, "channel_post"):
                    forward_from = str(fwd.from_id) if hasattr(fwd, "from_id") else None

            # Extract URLs from entities
            urls: list[str] = []
            if msg.entities:
                for entity in msg.entities:
                    if hasattr(entity, "url") and entity.url:
                        urls.append(entity.url)

            raw_dict = {
                "chat_id": chat_id,
                "message_id": msg.id,
                "sender": sender_name,
                "timestamp": msg.date,
                "raw_text": msg.text or msg.message or "",
                "media_type": detect_media_type(msg),
                "media_metadata": get_media_metadata(msg),
                "urls": urls or None,
                "is_forward": is_forward,
                "forward_from": forward_from,
                "ingest_time": utcnow(),
            }
            await self._queue.put(raw_dict)
            log.debug(
                "Enqueued message %d from chat %s (len=%d chars)",
                msg.id,
                chat_id,
                len(raw_dict["raw_text"]),
            )
        except Exception as exc:
            log.exception("Error handling Telegram event: %s", exc)

    async def run_until_disconnected(self) -> None:
        await self.client.run_until_disconnected()
