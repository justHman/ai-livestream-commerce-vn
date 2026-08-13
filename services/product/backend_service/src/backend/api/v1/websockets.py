"""backend.api.v1.websockets — /ws/control handler.

Copied from ``core/api/v1/websockets.py`` (COPY-DON'T-IMPORT, Task 1.25);
dependencies come from the typed ``BootstrapContainer`` (no media plane —
video flows renderer -> LiveKit -> browser directly). The former
``/ws/platform`` channel is removed (canonical ingress is now the
``POST /sessions/{id}/events`` endpoint).
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from backend.api.dependencies import container_from_websocket

from .auth import validate_ws_token
from .router import router, _allow_ws_message


@router.websocket("/ws/control/{session_id}")
async def ws_control(ws: WebSocket, session_id: str) -> None:
    d = container_from_websocket(ws)
    # Task 7: validate token BEFORE accept(). On invalid, close with 4401
    # and return without accepting (no control.connected event leaks).
    cfg = d.config
    if cfg is not None and not validate_ws_token(ws, cfg):
        await ws.close(code=4401)
        return
    if d.hub is not None:
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
                        orch = entry["orchestrator"]
                        await orch.cancel(session_id)
                    await asyncio.to_thread(d.backend.interrupt, session_id)
                    if d.hub is not None:
                        await d.hub.emit(session_id, {"type": "avatar.interrupted"})
                except KeyError:
                    await ws.send_json({"type": "error", "detail": "unknown session_id"})
            elif mtype == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        if d.hub is not None:
            d.hub.disconnect(session_id)
