"""Unversioned health endpoints — live and active-engine ready.

Excluded from `contracts/v1/openapi.json` (they live outside v1).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", response_model=None)
def health_ready(request: Request) -> dict[str, str] | JSONResponse:
    if getattr(request.app.state, "engine_ready", False):
        return {"status": "ready"}
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "reason": "engine_unavailable"},
    )
