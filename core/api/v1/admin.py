"""core.api.v1.admin — debug mode, sandbox verification, admin config/health."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from ...config import AppConfig
from ...render.base import StartOptions
from fastapi import Depends, HTTPException

from ..auth import admin_auth, debug_enabled_dep
from .router import router, SandboxVerifyReq, deps, health_ready, logger, rate_limit_admin


class DebugStartReq(BaseModel):
    session_id: str = Field(max_length=128)
    interval_sec: float = 5.0  # how often to feed mock comments
    traffic_mode: str = Field(default="random", max_length=32)


@router.post("/debug/start")
async def debug_start(
    req: DebugStartReq,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Start debug mode: feed mock viewer comments + simulated traffic to the Director."""
    d = deps()
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    if not d.director.has(req.session_id):
        raise HTTPException(status_code=409, detail="call /lite/attach first")
    from ...debug.traffic_sim import TrafficSimulator

    sim = TrafficSimulator(
        director=d.director,
        hub=d.hub,
        session_id=req.session_id,
        interval_sec=req.interval_sec,
        mode=req.traffic_mode,
    )
    sim.start()
    # Store the sim so we can stop it later
    if not hasattr(deps(), "_debug_sims"):
        d._debug_sims = {}
    d._debug_sims[req.session_id] = sim
    await d.hub.emit(
        req.session_id,
        {"type": "debug.started", "mode": req.traffic_mode, "interval_sec": req.interval_sec},
    )
    return {
        "ok": True,
        "session_id": req.session_id,
        "mode": req.traffic_mode,
        "interval_sec": req.interval_sec,
    }


class DebugStopReq(BaseModel):
    session_id: str = Field(max_length=128)


@router.post("/debug/stop")
async def debug_stop(
    req: DebugStopReq,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
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
        return {
            "running": True,
            "mode": sim.mode,
            "interval_sec": sim.interval_sec,
            "msgs_sent": sim.msgs_sent,
            "cycles": sim.cycles,
        }
    return {"running": False}


@router.get("/debug/mock_products")
async def debug_mock_products(
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Return a mock product catalog for debug/testing."""
    from ...debug.mock_data import MOCK_PRODUCTS

    return {"products": [p for p in MOCK_PRODUCTS]}


@router.get("/debug/mock_viewer_msgs")
async def debug_mock_viewer_msgs(
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Return the pool of mock viewer messages for debug."""
    from ...debug.mock_data import MOCK_VIEWER_MSGS

    return {"count": len(MOCK_VIEWER_MSGS), "messages": MOCK_VIEWER_MSGS}


@router.get("/debug/clusters/{session_id}")
async def debug_clusters(
    session_id: str,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Re-cluster the session's rolling comments + return current clusters.

    Stage 2 visibility: the coordinator queue auto-reactive path needs a
    self-host renderer (Stage 3), so this endpoint lets the FE show the
    cluster state (gom cụm) without relying on coordinator auto-speak.
    """
    coordinator = deps().coordinator
    if coordinator is None or not coordinator.has(session_id):
        raise HTTPException(404, "session not attached to Director coordinator")
    try:
        snapshot = coordinator.cluster_snapshot(session_id)
    except KeyError as exc:
        raise HTTPException(404, "session not attached") from exc
    return {**snapshot, "queue_stats": coordinator.stats(session_id)}


# ── Admin ───────────────────────────────────────────────────────────


def _sandbox_layer_timeout() -> float:
    # Read through the package for monkeypatch parity (test sandbox swaps
    # core.api.v1.SANDBOX_LAYER_TIMEOUT_SEC).
    from . import SANDBOX_LAYER_TIMEOUT_SEC as _package_timeout

    return _package_timeout


@router.post("/admin/sandbox/verify")
async def verify_sandbox(
    payload: SandboxVerifyReq,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Run bounded verification and clean late provider results."""
    backend = deps().backend
    layers: list[dict[str, Any]] = []
    session_id: Optional[str] = None

    async def run_layer(name: str, operation, error: str):
        started = time.monotonic()
        worker = asyncio.create_task(asyncio.to_thread(operation))
        try:
            result = await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=_sandbox_layer_timeout(),
            )
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception:
            logger.warning("Sandbox verification failed layer=%s", name)
            layers.append(
                {
                    "name": name,
                    "status": "fail",
                    "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                    "error": error,
                }
            )
            return None, worker
        layers.append(
            {
                "name": name,
                "status": "pass",
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
        )
        return result, None

    async def cleanup_late_start(worker: asyncio.Task) -> None:
        try:
            result = await worker
            await asyncio.to_thread(backend.stop, result.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Sandbox late-session cleanup failed")

    try:
        probe = getattr(backend, "verify_credentials", None)
        if not callable(probe):
            layers.append(
                {
                    "name": "credentials",
                    "status": "fail",
                    "latency_ms": 0.0,
                    "error": "credential verification unavailable",
                }
            )
            return {
                "ready": False,
                "layers": layers
                + [
                    {"name": "connectivity", "status": "skipped", "latency_ms": 0.0},
                    {"name": "speech", "status": "skipped", "latency_ms": 0.0},
                ],
            }
        credentials, _ = await run_layer("credentials", probe, "credential verification failed")
        if credentials is None:
            return {
                "ready": False,
                "layers": layers
                + [
                    {"name": "connectivity", "status": "skipped", "latency_ms": 0.0},
                    {"name": "speech", "status": "skipped", "latency_ms": 0.0},
                ],
            }
        result, late_worker = await run_layer(
            "connectivity",
            lambda: backend.start(StartOptions(avatar_id=payload.avatar_id, is_sandbox=True)),
            "LiveAvatar or LiveKit connectivity failed",
        )
        if result is None:
            if late_worker is not None:
                asyncio.create_task(cleanup_late_start(late_worker))
            return {
                "ready": False,
                "layers": layers + [{"name": "speech", "status": "skipped", "latency_ms": 0.0}],
            }
        session_id = result.session_id
        spoken, _ = await run_layer(
            "speech",
            lambda: backend.say(session_id, payload.speech_text, generate=True),
            "speech verification failed",
        )
        return {"ready": spoken is not None, "layers": layers}
    finally:
        if session_id is not None:
            try:
                await asyncio.to_thread(backend.stop, session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Sandbox verification cleanup failed")


@router.get("/admin/config")
async def admin_config(_: None = Depends(admin_auth)) -> dict[str, Any]:
    """Sanitized config dump: present/missing for secrets, no secret values."""
    cfg = deps().config or AppConfig.from_env()

    def _present(val: str) -> str:
        return "present" if (val or "").strip() else "missing"

    return {
        "app_env": cfg.app_env,
        "render_backend": cfg.render_backend,
        "store_backend": cfg.store_backend,
        "director_enabled": cfg.director_enabled,
        "debug_enabled": cfg.debug_enabled,
        "pipecat_enabled": getattr(cfg, "pipecat_enabled", False),
        "lmcache_enabled": cfg.lmcache_enabled,
        "coverage_match_threshold": getattr(cfg, "coverage_match_threshold", 0.75),
        "llm": {
            "engine": cfg.llm.engine,
            "model": cfg.llm.model or None,
            "base_url": "present" if cfg.llm.base_url else "missing",
            "guided_json": getattr(cfg.llm, "guided_json", False),
            "stream": cfg.llm.stream,
        },
        "tts": {
            "engine": cfg.tts.engine,
            "model": cfg.tts.model or None,
            "base_url": "present" if cfg.tts.base_url else "missing",
            "preset_id": cfg.tts.preset_id,
        },
        "secrets": {
            "backend_api_token": _present(cfg.backend_api_token),
            "admin_api_token": _present(cfg.admin_api_token),
            "liveavatar_api_key": "present" if cfg.api_key_present else "missing",
            "livekit_api_key": _present(cfg.livekit_api_key),
            "livekit_api_secret": _present(cfg.livekit_api_secret),
            "avatar_base_url": _present(cfg.avatar_base_url),
            "livekit_url": _present(cfg.livekit_url),
        },
    }


@router.get("/admin/health")
async def admin_health(_: None = Depends(admin_auth)) -> dict[str, Any]:
    """Deep health — same payload as /health/ready."""
    return await health_ready()

