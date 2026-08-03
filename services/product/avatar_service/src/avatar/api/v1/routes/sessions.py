"""Avatar session lifecycle routes.

The session route calls the real `sessions.py` lifecycle owner (Task 1.31),
which coordinates the engine and LiveKit publisher. Authorized start returns
only browser-safe LiveKit URL + client token (Task 1.30).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from avatar.api.dependencies import (
    get_gpu_concurrency_limiter,
    get_sessions,
)
from avatar.api.security.authorization import require_scope
from avatar.api.security.rate_limit import GPUConcurrencyLimiter
from avatar.api.v1.schemas.common import ErrorResponse
from avatar.api.v1.schemas.sessions import (
    SessionCreateRequest,
    SessionStartResponse,
    SessionStatusResponse,
)
from avatar.engines.base import StartOptions
from avatar.sessions import SessionManager

router = APIRouter()


@router.post(
    "/sessions",
    response_model=SessionStartResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def create_session(
    body: SessionCreateRequest,
    _scope: str = Depends(require_scope("avatar.render")),
    sessions: SessionManager = Depends(get_sessions),
    limiter: GPUConcurrencyLimiter = Depends(get_gpu_concurrency_limiter),
) -> SessionStartResponse:
    """Start an avatar session, returning browser-safe LiveKit data."""
    with limiter:
        result = sessions.create(
            StartOptions(
                avatar_id=body.avatar_id,
                is_sandbox=body.is_sandbox,
                extra=body.extra,
            )
        )
    return SessionStartResponse(
        session_id=result.session_id,
        livekit_url=result.livekit_url,
        livekit_client_token=result.livekit_client_token,
        mode=result.mode,
    )


@router.get(
    "/sessions/{session_id}",
    response_model=SessionStatusResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def session_status(
    session_id: str,
    _scope: str = Depends(require_scope("avatar.admin")),
    sessions: SessionManager = Depends(get_sessions),
) -> SessionStatusResponse:
    """Return a session status."""
    try:
        state = sessions.status(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionStatusResponse(session_id=session_id, status=state)


@router.post(
    "/sessions/{session_id}/interrupt",
    status_code=204,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def interrupt_session(
    session_id: str,
    _scope: str = Depends(require_scope("avatar.render")),
    sessions: SessionManager = Depends(get_sessions),
) -> None:
    """Barge-in: stop the current utterance."""
    try:
        sessions.interrupt(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@router.post(
    "/sessions/{session_id}/stop",
    status_code=204,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def stop_session(
    session_id: str,
    _scope: str = Depends(require_scope("avatar.render")),
    sessions: SessionManager = Depends(get_sessions),
) -> None:
    """Stop and clean up a session."""
    try:
        sessions.stop(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")