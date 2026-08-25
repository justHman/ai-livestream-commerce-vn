"""backend.api.health — operational health endpoints (OpenSpec 1.21 copy).

Copied from ``core/api/health.py`` (COPY-DON'T-IMPORT) so the canonical
backend service is self-contained; readiness reads the typed
``BootstrapContainer`` directly (no legacy v1 deps seam).

Canonical readiness semantics (audit R0.4): ready -> HTTP 200, not ready ->
HTTP 503. The diagnostic JSON body is returned on both statuses; the HTTP
status is the contract and a body can never override it. ``/health/live`` is
liveness only (process alive, no dependency checks) and stays 200 while the
process lives. ``/api/v1/health/ready`` (``backend.api.v1.router``) and
``/admin/health`` (``backend.api.v1.admin``) are explicit aliases of this
canonical route.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health/live")
async def health_live() -> dict[str, Any]:
    """Liveness probe — process is alive. Always 200, no deps check."""
    return {"ok": True, "status": "live"}


@router.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    """Canonical readiness probe — HTTP 200 only when ready, else 503.

    Checks that the app's ``BootstrapContainer`` is wired, that the
    configured render backend + engines are ready, and that any enabled
    embedder/postgres dependency is ready. Does NOT call external services.
    """
    container = getattr(request.app.state, "container", None)
    if container is None:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "status": "not_ready",
                "detail": "application state has no container",
            },
        )

    backend = container.backend
    backend_name = backend.name if backend is not None else None
    em = container.engine_manager
    llm_engine_name = "none"
    tts_engine_name = "tone"
    llm_loaded = False
    tts_loaded = False
    llm_load_error: str | None = None
    tts_load_error: str | None = None
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
        # was configured and FAILED to load, still report not-ready.
        ready = not (llm_load_error or tts_load_error)
    else:
        # Cloud / self-host: ready if engines are loaded OR the configured
        # engine is the stub (nothing is expected to load). A recorded load
        # failure overrides -> not-ready.
        llm_ok = llm_loaded or llm_engine_name in ("none", "", None)
        tts_ok = tts_loaded or tts_engine_name in ("tone", "", None)
        if llm_load_error:
            llm_ok = False
        if tts_load_error:
            tts_ok = False
        ready = llm_ok and tts_ok

    embedder: dict[str, Any] | None = None
    director = container.director
    if director is not None:
        from backend.application.director.embeddings import embedder_status

        try:
            embedder = embedder_status(director.embedder)
        except Exception as exc:
            embedder = {
                "name": "unavailable",
                "mode": "semantic-required",
                "ready": False,
                "degraded": False,
                "error": type(exc).__name__,
            }
        if not embedder["ready"]:
            ready = False
        if (
            embedder["degraded"]
            and container.config is not None
            and container.config.app_env != "dev"
        ):
            ready = False

    resp: dict[str, Any] = {
        "ok": ready,
        "status": "ready" if ready else "not_ready",
        "render_backend": backend_name,
        "llm_engine": llm_engine_name,
        "tts_engine": tts_engine_name,
    }
    if embedder is not None:
        resp["embedder"] = embedder
    if llm_load_error:
        resp["llm_load_error"] = llm_load_error
    if tts_load_error:
        resp["tts_load_error"] = tts_load_error
    pg = container.pg_store
    if pg is not None and getattr(pg, "enabled", False):
        try:
            pg_ok, pg_error = await pg.health()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Postgres readiness check failed error_type=%s", type(exc).__name__)
            pg_ok, pg_error = False, type(exc).__name__
        resp["postgres"] = "ready" if pg_ok else "not_ready"
        if not pg_ok:
            resp["ok"] = False
            resp["status"] = "not_ready"
            resp["postgres_error"] = pg_error

    return JSONResponse(status_code=200 if resp["ok"] else 503, content=resp)
