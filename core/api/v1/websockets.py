"""core.api.v1.websockets — /ws/control + /ws/platform handlers."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from ...render.orchestrator import StreamOrchestrator
from ..auth import validate_ws_token
from .router import router, _allow_ws_message, deps


@router.websocket("/ws/control/{session_id}")
async def ws_control(ws: WebSocket, session_id: str) -> None:
    d = deps()
    # Task 7: validate token BEFORE accept(). On invalid, close with 4401
    # and return without accepting (no control.connected event leaks).
    cfg = d.config
    if cfg is not None and not validate_ws_token(ws, cfg):
        await ws.close(code=4401)
        return
    await d.hub.connect(session_id, ws)
    connection_id = str(uuid.uuid4())
    await ws.send_json({"type": "control.connected", "session_id": session_id})
    try:
        while True:
            msg = await ws.receive_json()
            if not await _allow_ws_message(ws, "viewer", session_id, connection_id):
                return
            mtype = msg.get("type")
            if mtype == "interrupt":
                try:
                    # Task 8: cancel any active streaming orchestrator first.
                    entry = d.orchestrators.get(session_id)
                    if entry is not None:
                        orch: StreamOrchestrator = entry["orchestrator"]
                        await orch.cancel(session_id)
                    await asyncio.to_thread(d.backend.interrupt, session_id)
                    await d.hub.emit(session_id, {"type": "avatar.interrupted"})
                except KeyError:
                    await ws.send_json({"type": "error", "detail": "unknown session_id"})
            elif mtype == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        d.hub.disconnect(session_id)


@router.websocket("/ws/platform/{session_id}")
async def ws_platform(ws: WebSocket, session_id: str) -> None:
    """Accept platform chat JSON {text, author?} → coordinator ChatQueue."""
    d = deps()
    cfg = d.config
    if cfg is not None and not validate_ws_token(ws, cfg):
        await ws.close(code=4401)
        return
    await ws.accept()
    connection_id = str(uuid.uuid4())
    await ws.send_json({"type": "platform.connected", "session_id": session_id})
    try:
        while True:
            msg = await ws.receive_json()
            if not await _allow_ws_message(ws, "viewer", session_id, connection_id):
                return
            text = msg.get("text")
            if not isinstance(text, str) or not (text := text.strip()):
                await ws.send_json({"type": "error", "detail": "text required"})
                continue
            if len(text) > 500:
                await ws.send_json({"type": "error", "detail": "text too long"})
                continue
            author = msg.get("author") or "viewer"
            if not isinstance(author, str) or not author or len(author) > 128:
                await ws.send_json({"type": "error", "detail": "invalid author"})
                continue
            ts = msg.get("ts")
            if d.coordinator is not None and d.coordinator.has(session_id):
                try:
                    comment = d.coordinator.ingest(session_id, text, author=author, ts=ts)
                    await ws.send_json(
                        {
                            "type": "platform.accepted",
                            "comment_id": comment.id,
                        }
                    )
                except KeyError:
                    await ws.send_json({"type": "error", "detail": "session not attached"})
            else:
                # Store pending message on session meta when coordinator absent.
                meta = await d.store.get(session_id) or {}
                pending = list(meta.get("pending_platform_chat") or [])
                pending.append({"text": text, "author": author, "ts": ts})
                meta["pending_platform_chat"] = pending[-100:]
                await d.store.set(session_id, meta)
                await ws.send_json({"type": "platform.stored", "pending": len(pending)})
    except WebSocketDisconnect:
        return
