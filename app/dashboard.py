"""FastAPI dashboard application.

Provides:
  - GET /              → HTML dashboard
  - GET /incidents/{id} → incident detail page
  - WS  /ws            → live event push to browser
  - GET /api/messages  → recent messages (JSON)
  - GET /api/incidents → active incidents (JSON)
  - GET /api/sources   → source stats (JSON)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from app import config
from app.storage import (
    get_all_enabled_sources,
    get_incident,
    get_recent_incidents,
    get_recent_messages,
    get_source_stats,
    session_scope,
)

log = logging.getLogger(__name__)

app = FastAPI(title="Beirut Incident Monitor", version="1.0.0")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ─── WebSocket connection manager ────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.append(ws)
        log.debug("WS client connected (%d total)", len(self._active))

    def disconnect(self, ws: WebSocket) -> None:
        self._active.remove(ws)
        log.debug("WS client disconnected (%d remaining)", len(self._active))

    async def broadcast(self, data: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        payload = json.dumps(data, default=str)
        for ws in list(self._active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self._active.remove(ws)
            except ValueError:
                pass


manager = ConnectionManager()


async def broadcast_event(data: dict[str, Any]) -> None:
    """Called from the ingest pipeline to push live events to dashboard clients."""
    await manager.broadcast(data)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
async def incident_detail_page(request: Request, incident_id: int):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "highlight_incident_id": incident_id},
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send initial data snapshot on connect
        async with session_scope() as session:
            incidents = await get_recent_incidents(session, limit=20)
            messages = await get_recent_messages(session, limit=50)

        snapshot = {
            "type": "snapshot",
            "incidents": [_incident_to_dict(i) for i in incidents],
            "messages": [_message_to_dict(m) for m in messages],
        }
        await websocket.send_text(json.dumps(snapshot, default=str))

        # Keep alive – wait for client to disconnect
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.get("/api/messages")
async def api_messages(
    limit: int = Query(default=100, le=500),
    lang: str | None = Query(default=None),
    source_id: int | None = Query(default=None),
    min_score: float | None = Query(default=None),
):
    async with session_scope() as session:
        msgs = await get_recent_messages(
            session,
            limit=limit,
            lang_filter=lang,
            source_id=source_id,
            min_score=min_score,
        )
    return [_message_to_dict(m) for m in msgs]


@app.get("/api/incidents")
async def api_incidents(
    limit: int = Query(default=50, le=200),
    window_seconds: int | None = Query(default=None),
):
    async with session_scope() as session:
        incidents = await get_recent_incidents(session, window_seconds=window_seconds, limit=limit)
    return [_incident_to_dict(i) for i in incidents]


@app.get("/api/incidents/{incident_id}")
async def api_incident_detail(incident_id: int):
    async with session_scope() as session:
        incident = await get_incident(session, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    d = _incident_to_dict(incident)
    d["messages"] = [
        _message_to_dict(link.message)
        for link in (incident.messages or [])
        if link.message
    ]
    return d


@app.get("/api/sources")
async def api_sources():
    async with session_scope() as session:
        stats = await get_source_stats(session)
    return stats


# ─── Serialisation helpers ───────────────────────────────────────────────────

def _message_to_dict(m: Any) -> dict:
    return {
        "id": m.id,
        "source_id": m.source_id,
        "source_name": m.source.name if m.source else None,
        "trust_tier": m.source.trust_tier if m.source else None,
        "chat_id": m.chat_id,
        "message_id": m.message_id,
        "sender": m.sender,
        "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        "raw_text": m.raw_text,
        "detected_language": m.detected_language,
        "english_translation": m.english_translation,
        "media_type": m.media_type,
        "is_forward": m.is_forward,
        "relevance_score": m.relevance_score,
        "is_relevant": m.is_relevant,
    }


def _incident_to_dict(i: Any) -> dict:
    return {
        "id": i.id,
        "status": i.status,
        "first_seen": i.first_seen.isoformat() if i.first_seen else None,
        "last_updated": i.last_updated.isoformat() if i.last_updated else None,
        "confidence_score": i.confidence_score,
        "summary": i.summary,
        "location_hint": i.location_hint,
        "notes": i.notes,
        "external_url": i.external_url,
    }
