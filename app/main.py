"""Application entry point.

Starts three concurrent tasks:
  1. Telegram listener  – receives messages onto the queue
  2. Ingest pipeline    – processes messages from the queue
  3. Dashboard server   – FastAPI / uvicorn web server

Run with:
  python -m app.main
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import uvicorn

from app import config
from app.alerts import set_telegram_client
from app.auth import register_auth_routes
from app.dashboard import app as dashboard_app, broadcast_event
from app.ingest import run_pipeline, set_broadcast_fn
from app.storage import get_session_factory, init_db, session_scope, upsert_source
from app.telegram_client import TelegramMonitorClient
from app.utils import setup_logging

log = logging.getLogger(__name__)


async def _setup_sources(sources_config: list) -> None:
    """Persist source definitions to the database."""
    async with session_scope() as session:
        for src in sources_config:
            await upsert_source(session, src)
    log.info("Loaded %d source(s) into database", len(sources_config))


async def main() -> None:
    setup_logging(config.LOG_LEVEL)

    # Ensure data directory exists
    Path("data").mkdir(exist_ok=True)

    # Initialise database
    await init_db()

    # Load sources
    from app.config import load_sources
    sources = load_sources()
    enabled = [s for s in sources if s.enabled]
    log.info("Found %d enabled source(s) in sources.yaml", len(enabled))
    await _setup_sources(enabled)

    # Message queue shared between Telegram client and ingest pipeline
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    # Wire up the broadcast function so ingest can push to dashboard WS clients
    set_broadcast_fn(broadcast_event)

    # Register auth routes (for first-time Telegram login via browser)
    register_auth_routes(dashboard_app)

    # ── Dashboard server (starts immediately, regardless of Telegram status) ──
    uvicorn_config = uvicorn.Config(
        app=dashboard_app,
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        log_level="warning",
        loop="asyncio",
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)

    # ── Telegram client (non-fatal if it fails) ───────────────────────────────
    tg_client = TelegramMonitorClient(queue)
    tg_connected = False
    try:
        await tg_client.start()
        tg_connected = True
        set_telegram_client(tg_client)
        resolved = await tg_client.join_sources(enabled)
        log.info("Watching %d channel(s): %s", len(resolved), list(resolved.keys()))
        async with session_scope() as session:
            for src in enabled:
                if src.name in resolved:
                    src.chat_id = resolved[src.name]
                await upsert_source(session, src)
    except Exception as exc:
        log.error("Telegram client failed to start: %s", exc)
        log.warning("Dashboard will run without Telegram monitoring – retrying is not automatic")

    # ── Run tasks concurrently ────────────────────────────────────────────────
    async def _shutdown(signum, loop):
        log.info("Shutting down (signal %s)…", signum)
        if tg_connected:
            await tg_client.stop()
        uvicorn_server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_shutdown(s, loop)))
        except NotImplementedError:
            pass  # Windows

    log.info(
        "Beirut Monitor running – dashboard at http://%s:%s",
        config.DASHBOARD_HOST,
        config.DASHBOARD_PORT,
    )

    tasks = [run_pipeline(queue), uvicorn_server.serve()]
    if tg_connected:
        tasks.append(tg_client.run_until_disconnected())

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user")
