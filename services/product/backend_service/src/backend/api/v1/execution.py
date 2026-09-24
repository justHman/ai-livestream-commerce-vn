"""Execution contract endpoints for the current runtime session.

These endpoints record evidence and truthful command outcomes. They do not
activate autonomous speech, Hold/Resume or business lifecycle transitions.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Any

from fastapi import Depends, HTTPException, Request

from backend.api.dependencies import container_from_request
from backend.application.db.session_store import SessionLockTimeout
from backend.application.execution_contract import (
    Capabilities,
    CommandOutcome,
    CommandRequest,
    ContractRejection,
    Evidence,
    ExecutionState,
    apply_evidence,
    command_rejection,
)

from .router import router, viewer_auth
from .auth import admin_auth

_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def _locked(store: Any, session_id: str) -> AsyncIterator[Any]:
    distributed = getattr(store, "with_session_lock", None)
    if distributed is not None:
        try:
            async with distributed(session_id) as fence:
                yield fence
        except SessionLockTimeout as exc:
            raise HTTPException(status_code=503, detail={"code": "session_busy"}) from exc
    else:
        lock = _locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            yield None


async def _save(store: Any, session_id: str, meta: dict[str, Any], fence: Any) -> None:
    if fence is not None:
        if not await store.commit_if_owner(fence, meta):
            raise HTTPException(status_code=503, detail={"code": "session_busy"})
    else:
        await store.set(session_id, meta)


async def _load(store: Any, session_id: str) -> tuple[dict[str, Any], ExecutionState]:
    meta = await store.get(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_session"})
    raw = meta.get("execution_contract")
    if not raw:
        raise HTTPException(status_code=409, detail={"code": "unsupported_capability"})
    return meta, ExecutionState.model_validate(raw)


@router.get("/sessions/{session_id}/execution")
async def get_execution(
    session_id: str, request: Request, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    d = container_from_request(request)
    _, state = await _load(d.store, session_id)
    return {"state": state.model_dump(mode="json"), "capabilities": Capabilities().model_dump()}


@router.post("/sessions/{session_id}/execution/evidence")
async def record_execution_evidence(
    session_id: str, evidence: Evidence, request: Request, _: None = Depends(admin_auth)
) -> dict[str, Any]:
    d = container_from_request(request)
    async with _locked(d.store, session_id) as fence:
        meta, state = await _load(d.store, session_id)
        try:
            updated = apply_evidence(state, evidence)
        except ContractRejection as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
        meta["execution_contract"] = updated.model_dump(mode="json")
        await _save(d.store, session_id, meta, fence)
    return {"state": updated.model_dump(mode="json")}


@router.post("/sessions/{session_id}/execution/commands")
async def request_execution_command(
    session_id: str, command: CommandRequest, request: Request, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    d = container_from_request(request)
    async with _locked(d.store, session_id) as fence:
        meta, state = await _load(d.store, session_id)
        prior = meta.get("execution_command_outcomes", {}).get(command.command_id)
        if prior is not None:
            # An ID is bound to its original request, including actor and
            # execution generation; it cannot be replayed with new intent.
            original = CommandOutcome.model_validate(prior)
            if (
                original.model_dump(include=set(CommandRequest.model_fields))
                != command.model_dump()
            ):
                raise HTTPException(status_code=409, detail={"code": "duplicate_command_conflict"})
            return {"outcome": original.model_dump(mode="json"), "replayed": True}
        reason = command_rejection(state, command, Capabilities())
        # No command behavior is advertised by this task. A future supported
        # command must write an applied result only after its effect completes.
        outcome = CommandOutcome(
            **command.model_dump(),
            status="rejected",
            reason_code=reason or "unsupported_capability",
            result_at=datetime.now(timezone.utc),
        )
        meta.setdefault("execution_command_outcomes", {})[command.command_id] = outcome.model_dump(
            mode="json"
        )
        await _save(d.store, session_id, meta, fence)
    return {"outcome": outcome.model_dump(mode="json"), "replayed": False}
