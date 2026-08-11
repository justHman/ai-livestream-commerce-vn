"""Unversioned health endpoints — live and active-engine ready.

Excluded from `contracts/v1/openapi.json` (they live outside v1).

Change T: spec paths `GET /health` (process liveness) and `GET /ready`
(readiness) added alongside the legacy `/health/live` + `/health/ready`
aliases that existing tests depend on. Readiness stays false until the
active engine/provider subsystem is ready.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from tts.engines.base import EngineUnavailable

router = APIRouter()


def _liveness() -> dict[str, str]:
    return {"status": "ok"}


def _readiness(request: Request) -> dict[str, str]:
    if getattr(request.app.state, "engine_ready", False):
        return {"status": "ready"}
    return {"status": "not_ready", "reason": "engine_unavailable"}


@router.get("/health/live")
def health_live() -> dict[str, str]:
    return _liveness()


@router.get("/health")
def health() -> dict[str, str]:
    return _liveness()


@router.get("/health/ready")
def health_ready(request: Request) -> dict[str, str]:
    return _readiness(request)


@router.get("/ready")
def ready(request: Request) -> dict[str, str]:
    """Readiness probe: 503 (engine_unavailable) until the engine is ready."""
    if not getattr(request.app.state, "engine_ready", False):
        raise EngineUnavailable("engine not ready")
    return {"status": "ready"}
