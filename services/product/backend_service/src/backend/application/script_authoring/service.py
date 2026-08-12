"""ScriptAuthoringService protocol — the only dependency the API router binds.

Tasks 11.1-11.11 build the ``/api/v1/script-sets`` surface against this
protocol; the domain implementation (models/workflow/generation) is built by
parallel clusters and wired into the container by its own cluster. This module
exists so the router imports NO other ``script_authoring`` domain module.

Error model: domain failures raise ``ScriptAuthoringError`` carrying a stable
machine code (the router maps codes to HTTP status). Codes are part of the
wire contract:

  - ``illegal_transition``    409  invalid state transition
  - ``not_found``             404  script set / product / batch / version
  - ``stale_revision``        409  optimistic-lock mismatch
  - ``fix_not_eligible``      409  AI fix requested on a non-gate-failed version
  - ``missing_or_stale_script`` 409  binding-time missing/stale script
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

__all__ = ["ScriptAuthoringError", "ScriptAuthoringService"]


class ScriptAuthoringError(Exception):
    """Domain error with a stable machine code (wire contract)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ScriptAuthoringService(Protocol):
    """Container-scoped authoring capability consumed by ``api/v1/scripts``.

    All commands are async; long-running generation/fix/regenerate commands
    create finite workflows and return immediately. Implementations must keep
    event payloads free of raw script text by default (Decision 21).
    """

    # ── ScriptSet aggregate CRUD ─────────────────────────────────────

    async def create_script_set(
        self,
        *,
        name: str,
        transition_policy: str,
        product_ids: list[str],
        brief: dict[str, Any] | None,
    ) -> dict[str, Any]: ...

    async def get_script_set(self, *, set_id: str) -> dict[str, Any] | None: ...

    async def update_script_set(
        self,
        *,
        set_id: str,
        name: str | None,
        transition_policy: str | None,
        product_ids: list[str] | None,
        brief: dict[str, Any] | None,
        revision: int | None,
    ) -> dict[str, Any] | None: ...

    # ── Per-product commands ─────────────────────────────────────────

    async def save_draft(
        self,
        *,
        set_id: str,
        product_id: str,
        display_text: str,
        spoken_text: str | None,
        revision: int | None,
    ) -> dict[str, Any] | None: ...

    async def submit_for_gate(
        self, *, set_id: str, product_id: str
    ) -> dict[str, Any] | None: ...

    async def preview_product(
        self, *, set_id: str, product_id: str, target_duration_s: int
    ) -> dict[str, Any] | None: ...

    async def start_generation(
        self,
        *,
        set_id: str,
        product_id: str,
        target_duration_s: int,
        intent: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def regenerate_segment(
        self,
        *,
        set_id: str,
        product_id: str,
        segment_index: int,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def fix_with_ai(
        self,
        *,
        set_id: str,
        product_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def approve_product(
        self,
        *,
        set_id: str,
        product_id: str,
        version_id: str,
        actor: str,
    ) -> dict[str, Any] | None: ...

    async def approve_batch(
        self,
        *,
        set_id: str,
        product_ids: list[str],
        version_ids: dict[str, str],
        actor: str,
    ) -> dict[str, Any] | None: ...

    # ── Batch ────────────────────────────────────────────────────────

    async def start_batch_generation(
        self,
        *,
        set_id: str,
        product_ids: list[str],
        target_duration_s: int,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def get_batch(
        self, *, set_id: str, batch_id: str
    ) -> dict[str, Any] | None: ...

    async def cancel_batch(
        self, *, set_id: str, batch_id: str
    ) -> dict[str, Any] | None: ...

    # ── SSE (task 11.10) ─────────────────────────────────────────────

    async def get_batch_events_snapshot(
        self, *, set_id: str, batch_id: str
    ) -> str | None:
        """JSON payload of the reconnect snapshot (first SSE event).

        Carries the batch state plus a monotonic ``revision``; the snapshot
        must be a stable single value so replay is deduplicable.
        """
        ...

    async def stream_batch_events(
        self, *, set_id: str, batch_id: str
    ) -> AsyncIterator[dict[str, str]]:
        """Async iterator of ``{"event": str, "data": str}`` live events.

        Events carry stable IDs (set/batch/product/segment) and a monotonic
        sequence; payloads contain no script text by default.
        """
        ...
        yield {}
