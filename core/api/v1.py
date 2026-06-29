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

    async def broadcast(self, event: dict) -> None:
        """Send an event to ALL connected sessions (engine swap notifications)."""
        dead = []
        for sid, ws in self._conns.items():
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(sid)
        for sid in dead:
            self.disconnect(sid)


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

    def __init__(self, backend: RenderBackend, store, hub: ControlHub, director=None,
                 engine_manager=None) -> None:
        self.backend = backend
        self.store = store
        self.hub = hub
        self.director = director       # DirectorRuntime (optional)
        self.engine_manager = engine_manager  # EngineManager (optional, for runtime swap)


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


# ── Engine management endpoints (runtime LLM/TTS swap) ───────────────


class EngineSwapReq(BaseModel):
    engine: str
    model: str = ""
    model_path: str = ""
    device: str = "auto"
    # LLM-specific
    n_ctx: int = 4096
    n_gpu_layers: int = -1
    max_model_len: int = 4096
    max_tokens: int = 128
    temperature: float = 0.7
    quantization: Optional[str] = None
    # TTS-specific
    sample_rate: int = 24000
    ref_audio: Optional[str] = None
    # Extra passthrough
    extra: dict[str, Any] = {}


@router.get("/engines")
async def engines_status() -> dict[str, Any]:
    """List available LLM/TTS presets + currently loaded engines."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    return d.engine_manager.status()


@router.post("/engines/llm")
async def swap_llm(req: EngineSwapReq) -> dict[str, Any]:
    """Swap the LLM engine at runtime. Unloads the old model (frees VRAM),
    loads the new one, re-configures the cloud RenderBackend."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    cfg = {
        "engine": req.engine,
        "model": req.model,
        "model_path": req.model_path,
        "device": req.device,
        "n_ctx": req.n_ctx,
        "n_gpu_layers": req.n_gpu_layers,
        "max_model_len": req.max_model_len,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "quantization": req.quantization,
    }
    cfg.update(req.extra)
    await d.hub.broadcast({"type": "engine.llm_swap_started", "engine": req.engine, "model": req.model})
    try:
        info = await asyncio.to_thread(d.engine_manager.load_llm, cfg)
        d.engine_manager.reconfigure_cloud()
    except Exception as exc:
        await d.hub.broadcast({"type": "engine.llm_swap_failed", "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await d.hub.broadcast({"type": "engine.llm_swapped", "engine": info.engine, "model": info.model})
    return {"ok": True, "engine": info.engine, "model": info.model, "name": info.name}


@router.post("/engines/tts")
async def swap_tts(req: EngineSwapReq) -> dict[str, Any]:
    """Swap the TTS engine at runtime. Unloads the old model (frees VRAM),
    loads the new one, re-configures the cloud RenderBackend."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    cfg = {
        "engine": req.engine,
        "model": req.model,
        "weights_path": req.model or req.model_path,
        "device": req.device,
        "sample_rate": req.sample_rate,
        "ref_audio": req.ref_audio,
    }
    cfg.update(req.extra)
    await d.hub.broadcast({"type": "engine.tts_swap_started", "engine": req.engine, "model": req.model})
    try:
        info = await asyncio.to_thread(d.engine_manager.load_tts, cfg)
        d.engine_manager.reconfigure_cloud()
    except Exception as exc:
        await d.hub.broadcast({"type": "engine.tts_swap_failed", "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await d.hub.broadcast({"type": "engine.tts_swapped", "engine": info.engine, "model": info.model,
                           "sample_rate": info.sample_rate})
    return {"ok": True, "engine": info.engine, "model": info.model, "name": info.name,
            "sample_rate": info.sample_rate}


# ── Debug mode endpoints (mock viewer traffic + products) ─────────────


class DebugStartReq(BaseModel):
    session_id: str
    interval_sec: float = 5.0       # how often to feed mock comments
    traffic_mode: str = "random"    # "random" | "low" | "medium" | "high" | "ramp"


@router.post("/debug/start")
async def debug_start(req: DebugStartReq) -> dict[str, Any]:
    """Start debug mode: feed mock viewer comments + simulated traffic to the Director."""
    d = deps()
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    if not d.director.has(req.session_id):
        raise HTTPException(status_code=409, detail="call /lite/attach first")
    from ..debug.traffic_sim import TrafficSimulator

    sim = TrafficSimulator(director=d.director, hub=d.hub, session_id=req.session_id,
                           interval_sec=req.interval_sec, mode=req.traffic_mode)
    sim.start()
    # Store the sim so we can stop it later
    if not hasattr(deps(), "_debug_sims"):
        d._debug_sims = {}
    d._debug_sims[req.session_id] = sim
    await d.hub.emit(req.session_id, {"type": "debug.started", "mode": req.traffic_mode,
                                      "interval_sec": req.interval_sec})
    return {"ok": True, "session_id": req.session_id, "mode": req.traffic_mode,
            "interval_sec": req.interval_sec}


class DebugStopReq(BaseModel):
    session_id: str


@router.post("/debug/stop")
async def debug_stop(req: DebugStopReq) -> dict[str, Any]:
    """Stop debug mode: stop the mock traffic simulator."""
    d = deps()
    sim = getattr(d, "_debug_sims", {}).pop(req.session_id, None)
    if sim is not None:
        sim.stop()
        await d.hub.emit(req.session_id, {"type": "debug.stopped"})
        return {"ok": True, "stopped": req.session_id}
    return {"ok": False, "detail": "no debug session running"}


@router.get("/debug/status/{session_id}")
async def debug_status(session_id: str) -> dict[str, Any]:
    """Check if debug mode is running for a session."""
    d = deps()
    sim = getattr(d, "_debug_sims", {}).get(session_id)
    if sim is not None:
        return {"running": True, "mode": sim.mode, "interval_sec": sim.interval_sec,
                "msgs_sent": sim.msgs_sent, "cycles": sim.cycles}
    return {"running": False}


@router.get("/debug/mock_products")
async def debug_mock_products() -> dict[str, Any]:
    """Return a mock product catalog for debug/testing."""
    from ..debug.mock_data import MOCK_PRODUCTS

    return {"products": [p for p in MOCK_PRODUCTS]}


@router.get("/debug/mock_viewer_msgs")
async def debug_mock_viewer_msgs() -> dict[str, Any]:
    """Return the pool of mock viewer messages for debug."""
    from ..debug.mock_data import MOCK_VIEWER_MSGS

    return {"count": len(MOCK_VIEWER_MSGS), "messages": MOCK_VIEWER_MSGS}


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
