"""Unversioned health endpoints — live and active-engine ready.

Liveness checks only process/event-loop survival (no dependencies).
Readiness checks the configured self-host engine. Health responses expose
no secret, internal stack, or provider failure detail, and are excluded
from `contracts/v1/openapi.json` (they live outside v1).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from llm.engines.base import EngineUnavailable

router = APIRouter()


@router.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def health_ready(request: Request) -> dict[str, str]:
    """Readiness probe — 503 until the engine is ready (audit R0.4).

    The ``EngineUnavailable`` exception handler maps this to HTTP 503 with an
    ``engine_unavailable`` JSON envelope; a not-ready body is never returned
    with HTTP 200.
    """
    if getattr(request.app.state, "engine_ready", False):
        return {"status": "ready"}
    raise EngineUnavailable("engine not ready")
