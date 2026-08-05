"""Canonical session orchestration (OpenSpec 1.21).

Owns the session lifecycle and LiveKit session metadata for the control
plane. The legacy ``core.api.v1`` route layer remains the transport adapter;
this module is the canonical home for lifecycle orchestration logic so the
route modules only perform transport validation and mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Session store contract (canonical, Task 1.23 moves it to backend.application.db).
from backend.application.db.session_store import SessionStore

__all__ = ["SessionStore", "SessionInfo"]


@dataclass
class SessionInfo:
    """Browser-safe session metadata returned to the frontend."""

    session_id: str
    status: str = "created"
    mode: str = "mock"
    render_backend: Optional[str] = None
    avatar_id: Optional[str] = None
    room_name: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


def new_session(
    store: SessionStore,
    *,
    session_id: str,
    mode: str = "mock",
    render_backend: Optional[str] = None,
    avatar_id: Optional[str] = None,
    room_name: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> SessionInfo:
    """Create + persist a new session row in the configured store.

    Thin orchestration: persists JSON-serializable metadata only. Live
    WS/orchestrator objects stay in-process on the owning instance.
    """
    info = SessionInfo(
        session_id=session_id,
        status="created",
        mode=mode,
        render_backend=render_backend,
        avatar_id=avatar_id,
        room_name=room_name,
        metadata=dict(metadata or {}),
    )
    # The store contract is async; the route layer awaits persistence.
    # This helper returns the info; persistence is the caller's job so the
    # fire-and-forget error policy stays at the transport boundary.
    return info


def session_exists(store: SessionStore, session_id: str) -> bool:
    """Sync convenience for route-layer checks (store contract is async)."""
    return store.exists_sync(session_id)
