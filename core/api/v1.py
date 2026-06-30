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

from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import AppConfig
from ..llm.base import LLMEngine, LLMRequest, _NoopEngine
from ..render.base import FullPipelineBackend, RenderBackend, StartOptions, StreamingAvatarBackend
from ..render.mock import MockRenderBackend
from ..render.orchestrator import StreamOrchestrator
from ..render.queue import BoundedVideoQueue, CoordinatorMetrics
from ..render.locks import SessionLockRegistry
from ..tts.base import TTSEngine, ToneEngine
from .auth import admin_auth, debug_enabled_dep, validate_ws_token, viewer_auth

# Phase B coordinator — optional, constructed in server.py.
# Imported here for type annotations; actual import is safe (no heavy deps).
from ..director.coordinator import DirectorCoordinator

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


class ChatIn(BaseModel):
    """Wave 2: single chat comment from a viewer (Phase B coordinator path)."""
    session_id: str
    text: str
    author: str
    ts: Optional[float] = None


class TTSPresetIn(BaseModel):
    """Wave 2: select a TTS preset by id (Phase A dropdown)."""
    preset_id: str


# ── Wiring (set by core/server.py) ──────────────────────────────────


class V1Deps:
    """Dependencies injected by the server at startup."""

    def __init__(self, backend: RenderBackend, store, hub: ControlHub, director=None,
                 engine_manager=None, config: AppConfig | None = None,
                 locks: SessionLockRegistry | None = None,
                 orchestrators: dict | None = None,
                 coordinator: DirectorCoordinator | None = None) -> None:
        self.backend = backend
        self.store = store
        self.hub = hub
        self.director = director       # DirectorRuntime (optional)
        self.engine_manager = engine_manager  # EngineManager (optional, for runtime swap)
        self.config = config           # AppConfig (optional, for auth gates)
        # Task 8: per-session lock registry + active orchestrator map. Lazy
        # defaults so existing tests that build V1Deps directly still work.
        self.locks = locks if locks is not None else SessionLockRegistry()
        # session_id -> {"orchestrator": StreamOrchestrator, "queue": BoundedVideoQueue}
        self.orchestrators: dict = orchestrators if orchestrators is not None else {}
        # Phase B: DirectorCoordinator (optional). When present, /lite/chat
        # pushes comments into the coordinator's ChatQueue, and /lite/attach +
        # /lite/stop also drive the coordinator lifecycle.
        self.coordinator = coordinator


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


@router.get("/health/live")
async def health_live() -> dict[str, Any]:
    """Liveness probe — process is alive. Always 200, no deps check."""
    return {"ok": True, "status": "live"}


@router.get("/health/ready")
async def health_ready() -> dict[str, Any]:
    """Readiness probe — backend + engines are ready to serve.

    For the mock backend, readiness = backend exists (the mock renderer can
    always serve frames). For the cloud backend, readiness also requires the
    LLM/TTS engines to be loaded (or that the configured engine is the stub
    "none"/"tone" — in which case nothing is expected to load).

    Finding 2: if a configured REAL engine was requested but FAILED to load
    (``engine_manager.llm_load_error`` / ``tts_load_error`` set), readiness
    is ``False`` and the error is surfaced in the response — even though the
    server fell back to a stub so it could start. A prod deployment with a
    broken GGUF must NOT show "ready" while running echo stubs.
    """
    d = deps()
    backend = d.backend
    backend_name = backend.name if backend is not None else None
    em = d.engine_manager
    llm_engine_name = "none"
    tts_engine_name = "tone"
    llm_loaded = False
    tts_loaded = False
    llm_load_error: Optional[str] = None
    tts_load_error: Optional[str] = None
    if em is not None:
        if em.llm is not None:
            llm_loaded = True
            llm_engine_name = em.llm.name
        else:
            llm_engine_name = em.llm_cfg.get("engine", "none") or "none"
        if em.tts is not None:
            tts_loaded = True
            tts_engine_name = em.tts.name
        else:
            tts_engine_name = em.tts_cfg.get("engine", "tone") or "tone"
        llm_load_error = getattr(em, "llm_load_error", None)
        tts_load_error = getattr(em, "tts_load_error", None)

    if backend is None:
        ready = False
    elif backend_name == "mock":
        # Mock backend can always serve frames. But if a real LLM/TTS engine
        # was configured and FAILED to load (Finding 2), still report
        # not-ready so the operator sees the broken config.
        ready = not (llm_load_error or tts_load_error)
    else:
        # Cloud / self-host: ready if engines are loaded OR the configured
        # engine is the stub (nothing is expected to load). A recorded load
        # failure (Finding 2) overrides → not-ready.
        llm_ok = llm_loaded or llm_engine_name in ("none", "", None)
        tts_ok = tts_loaded or tts_engine_name in ("tone", "", None)
        if llm_load_error:
            llm_ok = False
        if tts_load_error:
            tts_ok = False
        ready = llm_ok and tts_ok

    resp: dict[str, Any] = {
        "ok": ready,
        "status": "ready" if ready else "not_ready",
        "render_backend": backend_name,
        "llm_engine": llm_engine_name,
        "tts_engine": tts_engine_name,
    }
    if llm_load_error:
        resp["llm_load_error"] = llm_load_error
    if tts_load_error:
        resp["tts_load_error"] = tts_load_error
    return resp


@router.post("/lite/start")
async def lite_start(req: StartReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
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
async def lite_say(req: SayReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    d = deps()
    # Phase E: streaming coordinator path for StreamingAvatarBackend (mock +
    # future self-host). FullPipelineBackend (cloud) keeps backend.say().
    if isinstance(d.backend, StreamingAvatarBackend):
        return await _streaming_say(d, req)
    if not isinstance(d.backend, FullPipelineBackend):
        raise HTTPException(status_code=501, detail="backend does not support say()")
    # Cloud / FullPipelineBackend path — unchanged.
    await d.hub.emit(req.session_id, {"type": "avatar.speak_started", "text": req.text})
    try:
        reply = await asyncio.to_thread(d.backend.say, req.session_id, req.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    await d.hub.emit(req.session_id, {"type": "avatar.speak_ended", "reply": reply})
    return {"ok": True, "reply": reply}


async def _streaming_say(d: V1Deps, req: SayReq) -> dict[str, Any]:
    """Run the LLM->chunker->TTS->backend streaming coordinator for one turn.

    Per-session lock: if a turn is already running for this session, reject
    with 409 already_speaking. The lock is released in ``finally`` so a
    subsequent say always succeeds once this one finishes.
    """
    sid = req.session_id
    if not d.locks.try_acquire(sid):
        raise HTTPException(status_code=409, detail="already_speaking")

    # Resolve LLM/TTS engines. Mock mode may have none loaded (e.g. offline
    # defaults LLM_ENGINE=none / TTS_ENGINE=tone) — fall back to the built-in
    # offline stubs so /lite/say always works without a model. When a real
    # engine IS configured in mock mode, server.py loads it and em.llm/.tts
    # are non-None (Finding 1).
    em = d.engine_manager
    llm: LLMEngine = em.llm if (em is not None and em.llm is not None) else _NoopEngine()
    tts: TTSEngine = em.tts if (em is not None and em.tts is not None) else ToneEngine()
    # Note: if a real engine was configured but FAILED to load, em.llm/.tts
    # are None and we fall back to the stubs here so /lite/say still responds.
    # /health/ready is the honest signal that the configured engine is broken
    # (Finding 2); this path keeps the server functional for dev/demo.

    # Bounded queue + metrics for this utterance.
    cfg = d.config or AppConfig()
    max_q = getattr(cfg, "avatar_max_queue_windows", 5)
    queue = BoundedVideoQueue(max_size=max_q)
    metrics = CoordinatorMetrics()
    orch_cfg = {
        "text_chunk_min_chars": getattr(cfg, "text_chunk_min_chars", 12),
        "text_chunk_target_chars": getattr(cfg, "text_chunk_target_chars", 40),
        "text_chunk_max_chars": getattr(cfg, "text_chunk_max_chars", 80),
        "text_chunk_flush_timeout_ms": getattr(cfg, "text_chunk_flush_timeout_ms", 350),
    }
    # Widen the try/finally to wrap the speak_started emit, orchestrator
    # construction, run, drain, and return. If the emit raises (e.g. WS
    # error), the finally still releases the per-session lock and cleans up
    # the orchestrator entry — otherwise the session would be permanently
    # locked (every future /lite/say -> 409) and the entry would leak.
    try:
        orchestrator = StreamOrchestrator(
            llm=llm, tts=tts, backend=d.backend, queue=queue, metrics=metrics, config=orch_cfg,
        )
        # Register the orchestrator so /lite/interrupt can cancel it.
        d.orchestrators[sid] = {"orchestrator": orchestrator, "queue": queue}

        await d.hub.emit(sid, {"type": "avatar.speak_started", "text": req.text})
        system_prompt = None
        if em is not None and hasattr(em, "_system_prompt"):
            system_prompt = em._system_prompt or None
        spoken = await orchestrator.run(sid, req.text, system_prompt=system_prompt)
        # Drain the queue: emit one WS event per VideoWindow to the control
        # hub so connected clients see frame updates. In production the MEDIA
        # plane carries the actual video; this control event is for telemetry.
        windows_emitted = 0
        while queue.qsize() > 0:
            vw = await queue.get()
            windows_emitted += 1
            await d.hub.emit(sid, {
                "type": "avatar.video_window",
                "seq": vw.seq,
                "is_final": vw.is_final,
                "duration_ms": vw.duration_ms,
            })
        await d.hub.emit(sid, {"type": "avatar.speak_ended", "reply": spoken})
        return {
            "ok": True,
            "reply": spoken,
            "windows": windows_emitted,
            "metrics": metrics.to_dict(),
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    finally:
        d.orchestrators.pop(sid, None)
        d.locks.release(sid)


@router.post("/lite/interrupt")
async def lite_interrupt(req: SessionReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    d = deps()
    # Task 8: if there is an active streaming orchestrator for this session,
    # cancel it first (stops emission + drains the bounded queue).
    entry = d.orchestrators.get(req.session_id)
    if entry is not None:
        orch: StreamOrchestrator = entry["orchestrator"]
        await orch.cancel(req.session_id)
    try:
        await asyncio.to_thread(d.backend.interrupt, req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await d.hub.emit(req.session_id, {"type": "avatar.interrupted"})
    return {"ok": True}


@router.post("/lite/stop")
async def lite_stop(req: SessionReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    d = deps()
    # Wave 2: stop the DirectorCoordinator for this session (before teardown).
    if d.coordinator is not None and d.coordinator.has(req.session_id):
        d.coordinator.stop(req.session_id)
    try:
        await asyncio.to_thread(d.backend.stop, req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if d.director is not None:
        d.director.detach(req.session_id)
    await d.store.delete(req.session_id)
    await d.hub.emit(req.session_id, {"type": "session.stopped"})
    # P4 hardening: drop the per-session lock entry to prevent memory leak.
    d.locks.drop(req.session_id)
    return {"ok": True, "stopped": req.session_id}


# ── Director-driven endpoints (orchestration) ───────────────────────


@router.post("/lite/attach")
async def lite_attach(req: AttachReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
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
    # Wave 2: also start the DirectorCoordinator for this session.
    if d.coordinator is not None:
        d.coordinator.start(session_id=req.session_id, products=products)
    return {"ok": True, **info}


@router.post("/lite/ingest")
async def lite_ingest(req: IngestReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    """Feed viewer comments to the Director; it decides + the avatar speaks.

    This is the closed loop: comments -> cluster/score -> Decision ->
    background streaming pipeline. Frontend just POSTs raw comments; the avatar reacts.

    Wave 2: when a DirectorCoordinator is active for this session, route
    comments through it (async ChatQueue path) instead of the sync Director
    ingest. The coordinator's tick loop will decide and speak asynchronously.
    Falls back to the existing sync Director path when coordinator is None.
    """
    d = deps()
    # Wave 2: coordinator path (async tick loop drains the queue).
    if d.coordinator is not None and d.coordinator.has(req.session_id):
        for c in req.comments:
            d.coordinator.ingest(req.session_id, c.text, author="viewer", ts=c.t)
        return {"ok": True, "accepted": True,
                "queue_stats": d.coordinator.stats(req.session_id)}

    # Fallback: original sync Director path.
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


# ── Wave 2: single-comment chat endpoint (Phase B coordinator) ────────


@router.post("/lite/chat", status_code=202)
async def lite_chat(payload: ChatIn, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    """Accept a single viewer chat comment via the DirectorCoordinator.

    Returns 202 Accepted immediately; the coordinator's tick loop processes
    comments asynchronously. Returns 404 if the coordinator is not active or
    the session is not attached.
    """
    d = deps()
    if d.coordinator is None or not d.coordinator.has(payload.session_id):
        raise HTTPException(404, "session not attached to coordinator")
    if len(payload.text) > 500:
        raise HTTPException(413, "text too long")
    comment = d.coordinator.ingest(
        session_id=payload.session_id,
        text=payload.text,
        author=payload.author,
        ts=payload.ts,
    )
    return {
        "accepted": True,
        "comment_id": comment.id,
        "queue_stats": d.coordinator.stats(payload.session_id),
    }


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
async def engines_status(_: None = Depends(admin_auth)) -> dict[str, Any]:
    """List available LLM/TTS presets + currently loaded engines."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    return d.engine_manager.status()


@router.post("/engines/llm")
async def swap_llm(req: EngineSwapReq, _: None = Depends(admin_auth)) -> dict[str, Any]:
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
async def swap_tts(req: EngineSwapReq, _: None = Depends(admin_auth)) -> dict[str, Any]:
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


@router.post("/engines/tts/preset")
async def set_tts_preset(payload: TTSPresetIn, _: None = Depends(admin_auth)) -> dict[str, Any]:
    """Select a TTS preset by id (Phase A dropdown). Updates the EngineManager's
    in-memory TTS config without loading the model. The next ``POST /engines/tts``
    or full reload will apply it."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=503, detail="engine manager not ready")
    try:
        updated = d.engine_manager.apply_tts_preset(payload.preset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset {payload.preset_id}")
    return {"preset_id": payload.preset_id, "tts_cfg": updated}


# ── Debug mode endpoints (mock viewer traffic + products) ─────────────


class DebugStartReq(BaseModel):
    session_id: str
    interval_sec: float = 5.0       # how often to feed mock comments
    traffic_mode: str = "random"    # "random" | "low" | "medium" | "high" | "ramp"


@router.post("/debug/start")
async def debug_start(
    req: DebugStartReq,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
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
async def debug_stop(
    req: DebugStopReq,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Stop debug mode: stop the mock traffic simulator."""
    d = deps()
    sim = getattr(d, "_debug_sims", {}).pop(req.session_id, None)
    if sim is not None:
        sim.stop()
        await d.hub.emit(req.session_id, {"type": "debug.stopped"})
        return {"ok": True, "stopped": req.session_id}
    return {"ok": False, "detail": "no debug session running"}


@router.get("/debug/status/{session_id}")
async def debug_status(
    session_id: str,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Check if debug mode is running for a session."""
    d = deps()
    sim = getattr(d, "_debug_sims", {}).get(session_id)
    if sim is not None:
        return {"running": True, "mode": sim.mode, "interval_sec": sim.interval_sec,
                "msgs_sent": sim.msgs_sent, "cycles": sim.cycles}
    return {"running": False}


@router.get("/debug/mock_products")
async def debug_mock_products(
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Return a mock product catalog for debug/testing."""
    from ..debug.mock_data import MOCK_PRODUCTS

    return {"products": [p for p in MOCK_PRODUCTS]}


@router.get("/debug/mock_viewer_msgs")
async def debug_mock_viewer_msgs(
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Return the pool of mock viewer messages for debug."""
    from ..debug.mock_data import MOCK_VIEWER_MSGS

    return {"count": len(MOCK_VIEWER_MSGS), "messages": MOCK_VIEWER_MSGS}


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
    await ws.send_json({"type": "control.connected", "session_id": session_id})
    try:
        while True:
            msg = await ws.receive_json()
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


# ── Mock media endpoints (only when RENDER_BACKEND=mock) ────────────

_MJPEG_BOUNDARY = "mockmjpegboundary"


def _mock_backend() -> Optional[MockRenderBackend]:
    """Return the mock backend if the active backend is a MockRenderBackend.

    Returns None otherwise so route handlers can 404 cleanly.
    """
    backend = deps().backend
    if isinstance(backend, MockRenderBackend):
        return backend
    return None


@router.get("/mock/frame/{session_id}.png")
async def mock_frame_png(session_id: str) -> Response:
    """Return the latest rendered frame as a PNG.

    Only available when the active backend is MockRenderBackend.
    """
    mb = _mock_backend()
    if mb is None:
        raise HTTPException(status_code=404, detail="mock backend not active")
    try:
        png = await asyncio.to_thread(mb.get_last_frame_png, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return Response(content=png, media_type="image/png")


@router.get("/mock/video/{session_id}.mjpeg")
async def mock_video_mjpeg(session_id: str) -> StreamingResponse:
    """Continuous MJPEG stream (Wave 2 rewrite).

    Emits idle-loop frames at ~25 fps when no utterance is playing. When
    a ``BoundedVideoQueue`` is active for the session (an utterance is
    running through ``_streaming_say`` or the coordinator), utterance frames
    are served from the queue and fall back to idle on underflow.

    The stream runs until the client disconnects or the session is stopped.
    Only available when the active backend is MockRenderBackend.
    """
    import time as _time

    mb = _mock_backend()
    if mb is None:
        raise HTTPException(status_code=404, detail="mock backend not active")
    # Validate session exists.
    if session_id not in mb._sessions:
        raise HTTPException(status_code=404, detail="unknown session_id")

    d = deps()
    fps = mb.fps
    frame_interval_s = 1.0 / fps

    async def _gen():
        boundary = _MJPEG_BOUNDARY
        while True:
            # Check whether a streaming orchestrator queue exists for this
            # session (active utterance). If so, use get_or_idle for frame
            # pacing. Otherwise serve idle frames directly.
            entry = d.orchestrators.get(session_id)
            if entry is not None:
                queue: BoundedVideoQueue = entry["queue"]
                idle_fn = lambda: mb.get_idle_frame_jpeg(
                    session_id, int(_time.monotonic_ns() // 1_000_000)
                )
                try:
                    jpeg, _is_idle = await queue.get_or_idle(
                        idle_fn, timeout_ms=int(frame_interval_s * 1000)
                    )
                except (KeyError, asyncio.CancelledError):
                    break
            else:
                # Pure idle: serve pre-rendered idle frames at frame rate.
                try:
                    jpeg = mb.get_idle_frame_jpeg(
                        session_id, int(_time.monotonic_ns() // 1_000_000)
                    )
                except KeyError:
                    break
                await asyncio.sleep(frame_interval_s)

            header = (
                f"--{boundary}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n\r\n"
            ).encode("ascii")
            yield header + jpeg + b"\r\n"

    return StreamingResponse(
        _gen(),
        media_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
    )


@router.get("/mock/status/{session_id}")
async def mock_status(session_id: str) -> dict[str, Any]:
    """Return the current status of a mock render session as JSON."""
    mb = _mock_backend()
    if mb is None:
        raise HTTPException(status_code=404, detail="mock backend not active")
    try:
        status = await asyncio.to_thread(mb.session_status, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return {"session_id": session_id, "status": status, "backend": "mock"}
