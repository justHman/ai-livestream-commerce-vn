"""Persist-before-delete seam for P0-FB-019 (DISABLED slice).

``d.terminal_outcomes`` exists only when TERMINAL_OUTCOMES_ENABLED is set and a
durable store, callback URL and secret are configured. Without it both helpers
are no-ops, so the legacy stop path is unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from backend.application.terminal_outcomes import TerminalPersistError


async def persist_terminal_before_delete(d: Any, session_id: str) -> None:
    """Store the durable terminal record first; on failure keep hot state and report ending/retry."""
    terminal = getattr(d, "terminal_outcomes", None)
    if terminal is None:
        return
    try:
        await terminal.persist_before_delete(d.store, session_id)
    except TerminalPersistError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "terminal_persist_failed", "phase": "ending", "retry": True},
        ) from exc


async def terminal_retry_pending(d: Any, session_id: str) -> bool:
    """True when a previous stop tore the session down but could not persist its record."""
    terminal = getattr(d, "terminal_outcomes", None)
    return terminal is not None and await terminal.retry_pending(d.store, session_id)
