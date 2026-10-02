"""Persist-before-delete seam for P0-FB-019 (DISABLED slice).

``d.terminal_outcomes`` exists only when TERMINAL_OUTCOMES_ENABLED is set and a
durable store, callback URL and secret are configured. Without it the helpers
run exactly the legacy teardown and do nothing else, so the legacy stop path is
unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from fastapi import HTTPException

from backend.application.terminal_outcomes import TerminalCleanupRetry, TerminalPersistError


async def register_terminal_execution(d: Any, session_id: str, meta: Mapping[str, Any]) -> None:
    """At start: durably remember the P0 execution (no-op while disabled)."""
    terminal = getattr(d, "terminal_outcomes", None)
    if terminal is not None:
        await terminal.register(session_id, meta)


async def teardown_then_persist(d: Any, session_id: str) -> None:
    """LiveKit stop + director detach, then the durable terminal record.

    Disabled: exactly the legacy two steps, errors propagating unchanged.
    Enabled: a teardown failure keeps hot state and answers 503 ending/retry
    (bounded; then the failure is recorded as cleanup=failed), the observed
    cleanup result is what the record carries, and a record that cannot be
    stored also keeps hot state with 503 ending/retry.
    """
    terminal = getattr(d, "terminal_outcomes", None)
    error: str | None = None
    try:
        if d.livekit_publishers is not None:
            # Enabled only: the retry-preserving stop. Disabled keeps the original stop.
            retryable = getattr(d.livekit_publishers, "stop_retryable", None) if terminal else None
            await (retryable or d.livekit_publishers.stop)(session_id)
        if d.director is not None:
            d.director.detach(session_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if terminal is None:
            raise
        error = type(exc).__name__
    if terminal is None:
        return
    try:
        cleanup = await terminal.settle_cleanup(d.store, session_id, error)
    except TerminalCleanupRetry as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "terminal_cleanup_retry", "phase": "ending", "retry": True},
        ) from exc
    try:
        await terminal.persist_before_delete(d.store, session_id, cleanup)
    except TerminalPersistError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "terminal_persist_failed", "phase": "ending", "retry": True},
        ) from exc


async def terminal_retry_pending(d: Any, session_id: str) -> bool:
    """True when a previous stop tore the session down but could not finish its record."""
    terminal = getattr(d, "terminal_outcomes", None)
    return terminal is not None and await terminal.retry_pending(d.store, session_id)
