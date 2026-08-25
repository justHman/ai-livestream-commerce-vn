"""Unversioned health endpoints — live and active-engine ready.

Excluded from `contracts/v1/openapi.json` (they live outside v1).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from avatar.engines.base import EngineUnavailable

router = APIRouter()


@router.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def health_ready(request: Request) -> JSONResponse:
    """Readiness probe — truthful about stub vs self-host (audit R0.3).

    The ``engine_is_stub`` flag tags the mock-model stub built for
    ``AVATAR_ENGINE=none``: a stub NEVER reports a production-ready
    self-host signal (HTTP 503 with reason ``test_stub_only``). A real
    self-host engine that is not up also returns 503 via ``EngineUnavailable``;
    only a genuinely ready self-host engine returns HTTP 200.
    """
    if getattr(request.app.state, "engine_is_stub", True):
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "reason": "test_stub_only",
                "engine": "avatarforcing",
                "mode": "test_stub",
            },
        )
    if not getattr(request.app.state, "engine_ready", False):
        raise EngineUnavailable("engine not ready")
    return JSONResponse(
        status_code=200,
        content={"status": "ready", "engine": "avatarforcing", "mode": "self_host"},
    )
