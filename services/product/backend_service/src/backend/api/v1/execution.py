"""Execution contract endpoints for the current runtime session.

These endpoints record evidence and truthful command outcomes. Start activates
one approved opening. Rescue commands and business transitions remain separate.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Any

from fastapi import Depends, HTTPException, Request

from backend.api.dependencies import container_from_request
from backend.api.health import health_ready
from backend.application.db.session_store import SessionLockTimeout
from backend.application.execution_contract import (
    Capabilities,
    CommandOutcome,
    CommandRequest,
    ContractRejection,
    Evidence,
    ExecutionIdentity,
    ExecutionState,
    apply_evidence,
    command_rejection,
    start_command_id,
)
from backend.application.script_authoring.approved_speech import SpeechRejected

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
    # Memory storage must preserve the same commit boundary as serialized Redis.
    meta = copy.deepcopy(meta)
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
    return copy.deepcopy(meta), ExecutionState.model_validate(raw)


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
        if evidence.kind == "first_ai_broadcast":
            receipt = d.coordinator.opening_media(session_id) if d.coordinator else None
            if not receipt or any(
                (
                    receipt["opening_turn_id"] != evidence.opening_turn_id,
                    receipt["media_utterance_id"] != evidence.media_utterance_id,
                    receipt["approved_envelope_hash"] != state.approved_envelope_hash,
                )
            ):
                raise HTTPException(status_code=409, detail={"code": "uncorrelated_media"})
        if evidence.kind == "runtime_ready" and evidence.media_readiness is not None:
            try:
                envelope = await d.approved_speech.resolve(session_id)
                if not d.coordinator or not d.coordinator.has(session_id):
                    raise SpeechRejected("session_not_attached")
                state = state.model_copy(update={"approved_envelope_hash": envelope.fingerprint})
            except SpeechRejected as exc:
                raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
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
        if command.command == "start":
            return await _request_start(d, session_id, meta, state, command, fence, request)
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
        # Rescue commands remain unavailable; never report an applied effect.
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


async def _request_start(d, session_id, meta, state, command, fence, request):
    """Consume one semantic start under the existing session lock/fence.

    Persist intent before activation. An ambiguous accepted outcome is returned
    on retry; it is never permission to replay speech after a process loss.
    """

    def result(status, reason, opening=None):
        return CommandOutcome(
            **command.model_dump(),
            status=status,
            reason_code=reason,
            result_at=datetime.now(timezone.utc),
            opening_turn_id=opening,
        )

    reason = command_rejection(state, command, Capabilities())
    if reason is None and command.command_id != start_command_id(state):
        reason = "invalid_start_identity"
    if reason is not None:
        return {"outcome": result("rejected", reason).model_dump(mode="json"), "replayed": False}
    prior = meta.get("execution_command_outcomes", {}).get(command.command_id)
    if prior is not None:
        original = CommandOutcome.model_validate(prior)
        identity_fields = set(ExecutionIdentity.model_fields)
        if original.command != "start" or original.model_dump(
            include=identity_fields
        ) != command.model_dump(include=identity_fields):
            raise HTTPException(status_code=409, detail={"code": "duplicate_command_conflict"})
        # Same scoped semantic start may arrive with a different actor/time.
        # Return the original actor/result truthfully; never activate twice.
        return {"outcome": original.model_dump(mode="json"), "replayed": True}
    try:
        if state.phase != "ready" or not state.runtime_ready:
            raise SpeechRejected("runtime_not_ready")
        if state.media_readiness is None or not state.media_readiness.ready():
            raise SpeechRejected("media_not_ready")
        if state.healthy is False or (await health_ready(request)).status_code != 200:
            raise SpeechRejected("dependencies_not_ready")
        envelope = await d.approved_speech.resolve(session_id)
        if envelope.fingerprint != state.approved_envelope_hash:
            raise SpeechRejected("stale_readiness_binding")
        if not d.coordinator or not d.coordinator.has(session_id):
            raise SpeechRejected("session_not_attached")
        if d.director.get_session(session_id).approved_envelope != envelope:
            raise SpeechRejected("stale_attached_binding")
    except SpeechRejected as exc:
        return {"outcome": result("rejected", exc.code).model_dump(mode="json"), "replayed": False}
    opening = command.command_id + ":opening"
    outcome = result("accepted", "start_accepted", opening)
    outcomes = meta.setdefault("execution_command_outcomes", {})
    outcomes[command.command_id] = outcome.model_dump(mode="json")
    meta["execution_contract"] = state.model_copy(
        update={
            "start_command_id": command.command_id,
            "opening_turn_id": opening,
        }
    ).model_dump(mode="json")
    await _save(d.store, session_id, meta, fence)
    # This synchronous effect queues preparation, never waits for speech/media.
    # Existing stop() removes the coordinator; activation checks it again.
    try:
        d.coordinator.activate_approved(session_id, opening, envelope)
    except (SpeechRejected, KeyError):
        return {"outcome": outcome.model_dump(mode="json"), "replayed": False}
    outcome = result("applied", "opening_scheduled", opening)
    outcomes[command.command_id] = outcome.model_dump(mode="json")
    await _save(d.store, session_id, meta, fence)
    return {"outcome": outcome.model_dump(mode="json"), "replayed": False}
