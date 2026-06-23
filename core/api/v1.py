"""/api/v1 — stable public API surface.

This router is the production contract. It talks ONLY to the RenderBackend
interface, so it is identical whether the renderer is LiveAvatar cloud or a
future self-host model.

Two planes (see PRODUCTION.md):
  CONTROL (here): JSON + WebSocket — session lifecycle, say, interrupt, events.
  MEDIA (NOT here): avatar VIDEO flows LiveAvatar/renderer -> LiveKit -> browser.

Endpoints:
  GET  /api/v1/health
  POST /api/v1/lite/start      {avatar_id?, is_sandbox?} -> {session_id, livekit_url, livekit_client_token, mode}
  POST /api/v1/lite/say        {session_id, text}        -> {ok, reply}
  POST /api/v1/lite/interrupt  {session_id}              -> {ok}
  POST /api/v1/lite/stop       {session_id}              -> {ok}
  WS   /api/v1/ws/control/{session_id}                   <- events; -> {type:"interrupt"|"ping"}
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..render.base import RenderBackend, StartOptions

router = APIRouter(prefix="/api/v1")


# ── Control-plane WS hub (one connection per session) ───────────────


class ControlHub:
    def __init__(self) -> None:
        self._conns: dict[str, WebSocket] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[session_id] = ws

    def disconnect(self, session_id: str) -> None:
        self._conns.pop(session_id, None)

    async def emit(self, session_id: str, event: dict) -> None:
        ws = self._conns.get(session_id)
        if ws is not None:
            try:
                await ws.send_json(event)
            except Exception:
                self.disconnect(session_id)


# ── Request models ──────────────────────────────────────────────────


class StartReq(BaseModel):
    avatar_id: Optional[str] = None
    is_sandbox: bool = True


class SayReq(BaseModel):
    session_id: str
    text: str


class SessionReq(BaseModel):
    session_id: str


class ProductIn(BaseModel):
    id: str
    name: str
    description: str = ""
    price: Optional[int] = None
    original_price: Optional[int] = None
    promotion: Optional[str] = None
    colors: list[str] = []
    sizes: list[str] = []
    material: Optional[str] = None
    shipping: Optional[str] = None
    warranty: Optional[str] = None
    in_stock: bool = True
    stock_total: Optional[int] = None
    ref_image: Optional[str] = None
    features: list[str] = []


class AttachReq(BaseModel):
    session_id: str
    products: list[ProductIn]


class CommentIn(BaseModel):
    text: str
    t: Optional[float] = None


class IngestReq(BaseModel):
    session_id: str
    comments: list[CommentIn]
    viewer_count: Optional[int] = None
    msg_rate: Optional[float] = None


# ── Wiring (set by core/server.py) ──────────────────────────────────


class V1Deps:
    """Dependencies injected by the server at startup."""

    def __init__(self, backend: RenderBackend, store, hub: ControlHub, director=None) -> None:
        self.backend = backend
        self.store = store
        self.hub = hub
        self.director = director  # DirectorRuntime (optional)


_deps: Optional[V1Deps] = None


def init_deps(deps: V1Deps) -> None:
    global _deps
    _deps = deps


def deps() -> V1Deps:
    if _deps is None:
        raise RuntimeError("v1 router not initialized — call init_deps()")
    return _deps


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/health")
async def health() -> dict[str, Any]:
    from ..config import AppConfig

    cfg = AppConfig.from_env()
    return {
        "ok": True,
        "render_backend": deps().backend.name,
        "store_backend": cfg.store_backend,
        "api_key_loaded": cfg.api_key_present,
        "director_enabled": deps().director is not None,
    }


@router.post("/lite/start")
async def lite_start(req: StartReq) -> dict[str, Any]:
    d = deps()
    try:
        result = await asyncio.to_thread(
            d.backend.start,
            StartOptions(avatar_id=req.avatar_id, is_sandbox=req.is_sandbox),
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await d.store.set(result.session_id, {"status": "active", "mode": result.mode})
    return result.public_dict()  # frontend-safe only


@router.post("/lite/say")
async def lite_say(req: SayReq) -> dict[str, Any]:
    d = deps()
    await d.hub.emit(req.session_id, {"type": "avatar.speak_started", "text": req.text})
    try:
        reply = await asyncio.to_thread(d.backend.say, req.session_id, req.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await d.hub.emit(req.session_id, {"type": "avatar.speak_ended", "reply": reply})
    return {"ok": True, "reply": reply}


@router.post("/lite/interrupt")
async def lite_interrupt(req: SessionReq) -> dict[str, Any]:
    d = deps()
    try:
        await asyncio.to_thread(d.backend.interrupt, req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await d.hub.emit(req.session_id, {"type": "avatar.interrupted"})
    return {"ok": True}


@router.post("/lite/stop")
async def lite_stop(req: SessionReq) -> dict[str, Any]:
    d = deps()
    try:
        await asyncio.to_thread(d.backend.stop, req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if d.director is not None:
        d.director.detach(req.session_id)
    await d.store.delete(req.session_id)
    await d.hub.emit(req.session_id, {"type": "session.stopped"})
    return {"ok": True, "stopped": req.session_id}


# ── Director-driven endpoints (orchestration) ───────────────────────


@router.post("/lite/attach")
async def lite_attach(req: AttachReq) -> dict[str, Any]:
    """Attach a Director to a started session: build the FSM + embed the catalog."""
    d = deps()
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    from ..director.catalog import Product

    products = [Product(**p.model_dump()) for p in req.products]
    try:
        info = await asyncio.to_thread(d.director.attach, req.session_id, products)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, **info}


@router.post("/lite/ingest")
async def lite_ingest(req: IngestReq) -> dict[str, Any]:
    """Feed viewer comments to the Director; it decides + the avatar speaks.

    This is the closed loop: comments -> cluster/score -> Decision ->
    backend.say(). Frontend just POSTs raw comments; the avatar reacts.
    """
    d = deps()
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    if not d.director.has(req.session_id):
        raise HTTPException(status_code=409, detail="call /lite/attach first")

    raw = [c.model_dump() for c in req.comments]
    await d.hub.emit(req.session_id, {"type": "director.cycle_started"})
    try:
        result = await asyncio.to_thread(
            d.director.ingest, req.session_id, raw, req.viewer_count, req.msg_rate
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await d.hub.emit(req.session_id, {"type": "director.spoke", **result})
    return {"ok": True, **result}


@router.websocket("/ws/control/{session_id}")
async def ws_control(ws: WebSocket, session_id: str) -> None:
    d = deps()
    await d.hub.connect(session_id, ws)
    await ws.send_json({"type": "control.connected", "session_id": session_id})
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "interrupt":
                try:
                    await asyncio.to_thread(d.backend.interrupt, session_id)
                    await d.hub.emit(session_id, {"type": "avatar.interrupted"})
                except KeyError:
                    await ws.send_json({"type": "error", "detail": "unknown session_id"})
            elif mtype == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        d.hub.disconnect(session_id)
