"""backend.api.health — operational health endpoints (OpenSpec 1.21 copy).

Copied from ``core/api/health.py`` (COPY-DON'T-IMPORT) so the canonical
backend service is self-contained; readiness reads the typed
``BootstrapContainer`` directly (no legacy v1 deps seam).

Excluded from the versioned v1 contract: ``/health/live`` (liveness, no
dependencies) and ``/health/ready`` (readiness, checks configured
dependencies) are mounted on the app directly by the app factories
(``backend.bootstrap.app_factory``). The ready probe fails loud when the app
has no ``app.state.container`` (the typed ``BootstrapContainer``) — it never
calls external services.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/health/live")
async def health_live() -> dict[str, Any]:
    """Liveness probe — process is alive. Always 200, no deps check."""
    return {"ok": True, "status": "live"}


@router.get("/health/ready")
async def health_ready(request: Request) -> dict[str, Any]:
    """Readiness probe — configured dependencies are present.

    Checks that the app's ``BootstrapContainer`` is wired (fail loud with
    503 if missing) and that the configured render backend + engines report
    ready via the v1 deps. Does NOT call external services.
    """
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="application state has no container")
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
        ready = not (llm_load_error or tts_load_error)
    else:
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
