"""Execution contract endpoints for the current runtime session.

These endpoints record evidence and truthful command outcomes. Start activates
one approved opening. Rescue commands (P0-FB-016, C-RESCUE-CMD-001) apply
synchronously under the session lock; business transitions remain the API's.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Any

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.dependencies import container_from_request
from backend.api.health import health_ready
from backend.application.db.session_store import SessionLockTimeout
from backend.application.execution_contract import (
    RESCUE_EFFECTS,
    Capabilities,
    CommandOutcome,
    CommandRequest,
    ContractRejection,
    Evidence,
    ExecutionIdentity,
    ExecutionState,
    RescueCommandRequest,
    apply_evidence,
    apply_rescue,
    command_rejection,
    rescue_rejection,
    start_command_id,
)
from backend.application import budget_lease
from backend.application.script_authoring.approved_speech import SpeechRejected
from backend.application.usage_evidence import (
    EVIDENCE_TTL,
    UNSTAGED_KEY,
    UsageEvidenceRejected,
    UsageEvidenceUnavailable,
    holds_evidence,
)
from backend.application.usage_evidence.envelope import InvalidIdentity

from .router import router, viewer_auth
from .auth import admin_auth, viewer_or_admin_auth

logger = logging.getLogger(__name__)
_locks: dict[str, asyncio.Lock] = {}
_closing_tasks: dict[str, asyncio.Task] = {}


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
    # A meta that holds unresolved usage evidence is its only copy: it outlives the default TTL.
    keep = {"ttl_seconds": EVIDENCE_TTL} if holds_evidence(meta) else {}
    if fence is not None:
        if not await store.commit_if_owner(fence, meta, **keep):
            raise HTTPException(status_code=503, detail={"code": "session_busy"})
    else:
        await store.set(session_id, meta, **keep)


async def _stage_usage(
    d: Any, meta: dict[str, Any], prior: ExecutionState, updated: ExecutionState, cause: Any
) -> list:
    """P0-FB-017: stage usage evidence for an already-applied fact, before the save.

    Control plane only (the session lock is held). A no-op unless the usage-evidence
    service is wired (default off). If the durable row cannot be stored the fact is NOT
    saved: the caller answers 503 and the API retries the same evidence.
    """
    ue = getattr(d, "usage_evidence", None)
    if ue is None:
        return []
    if meta.get(UNSTAGED_KEY):
        # Order: earlier facts of this session are still deferred; a later one must not be
        # staged (and numbered) ahead of them. The caller retries once the sweeper drained.
        raise HTTPException(status_code=503, detail={"code": "usage_evidence_unavailable"})
    try:
        if isinstance(cause, Evidence):
            staged = await ue.stage_evidence(prior, updated, cause, meta)
        else:
            staged = await ue.stage_command(prior, updated, cause, meta)
        _stamp_usage(d, meta, staged)  # committed by the same atomic save as the state
        return staged
    except UsageEvidenceUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "usage_evidence_unavailable"}) from exc
    except UsageEvidenceRejected as exc:
        raise HTTPException(
            status_code=422, detail={"code": "usage_evidence_invalid_identity"}
        ) from exc


DEFER_DRAIN_BUDGET = 3.0  # seconds: the bounded staging attempt after a safety stop took effect


def _defer_usage(
    d: Any,
    meta: dict[str, Any],
    prior: ExecutionState,
    updated: ExecutionState,
    cause: Any,
    session_id: str,
) -> None:
    """SAFETY commands (Emergency End): the stop never depends on a database call.

    The facts are only RECORDED in the session meta, which the caller saves in the same
    atomic write as the state. They are staged afterwards (``_drain_soon`` on a bounded budget,
    then the sweeper), so a slow or dead outbox can neither refuse nor delay the stop.
    """
    ue = getattr(d, "usage_evidence", None)
    if ue is None:
        return
    try:
        entries = ue.defer_entries(prior, updated, cause)
    except InvalidIdentity:  # never reportable: do not block a stop
        logger.error("usage evidence skipped for a safety command session=%s", session_id)
        return
    if not entries:
        return
    # A later fact is appended: the drain stages them strictly in this order.
    # (Unreachable cap today: Emergency End applies once and emits one fact.)
    meta[UNSTAGED_KEY] = ((meta.get(UNSTAGED_KEY) or []) + entries)[-64:]
    ue.unstaged_sessions.add(session_id)


async def _drain_soon(d: Any, session_id: str) -> None:
    """After the stop took effect: try to stage the deferred facts, bounded; never raises."""
    sender = getattr(d, "usage_sender", None)
    if sender is not None:
        await run_with_deadline(sender.drain_session(session_id), DEFER_DRAIN_BUDGET)


def _log_detached(task: "asyncio.Future[Any]") -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning(
            "usage evidence attempt after a safety stop deferred error_type=%s",
            type(task.exception()).__name__,
        )


async def run_with_deadline(coro: Any, budget: float) -> bool:
    """Run ``coro`` for at most ``budget`` seconds as a DETACHED task; True if it finished.

    At the deadline the task is cancelled but its cancellation is NEVER awaited (a stalled
    database cancel / pool release must not delay the caller past the lock lease). Its result
    is swallowed by a callback. The task only uses fenced paths (session lock + fenced saves),
    so once the lease is gone it can no longer write.
    """
    task = asyncio.ensure_future(coro)
    task.add_done_callback(_log_detached)
    try:
        done, _ = await asyncio.wait({task}, timeout=budget)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not done:
        task.cancel()
        return False
    return True


def _stamp_usage(d: Any, meta: dict[str, Any], staged: list) -> None:
    """Put the committed-fact ids into the meta that the state save writes atomically."""
    if staged:
        d.usage_evidence.stamp(meta, staged)


def _write_not_landed(exc: BaseException) -> bool:
    """True only for a DEFINITE failure: the fence refused the write (503 session_busy).

    Anything else (a lost reply, a timeout, a cancellation) is ambiguous: the write may
    have landed, so the staged rows must survive for the sweeper to resolve by proof.
    """
    return (
        isinstance(exc, HTTPException)
        and exc.status_code == 503
        and (exc.detail or {}).get("code") == "session_busy"
    )


async def _settle_usage(
    d: Any, staged: list, *, saved: bool, failure: BaseException | None = None
) -> None:
    if staged:
        ue = d.usage_evidence
        if saved:
            await ue.commit(staged)
        elif failure is not None and _write_not_landed(failure):
            await ue.abort(staged)


async def _load_meta(store: Any, session_id: str) -> dict[str, Any]:
    meta = await store.get(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return copy.deepcopy(meta)


async def _load(store: Any, session_id: str) -> tuple[dict[str, Any], ExecutionState]:
    meta = await store.get(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_session"})
    raw = meta.get("execution_contract")
    if not raw:
        raise HTTPException(status_code=409, detail={"code": "unsupported_capability"})
    return copy.deepcopy(meta), ExecutionState.model_validate(raw)


def _rfc3339(epoch: float | None) -> str | None:
    """C-MEDIA-001 wire type: one RFC3339 UTC string (or null); the backend keeps epoch floats."""
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


@router.get("/sessions/{session_id}/execution")
async def get_execution(
    session_id: str, request: Request, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    d = container_from_request(request)
    meta, state = await _load(d.store, session_id)
    if state.phase == "closing" and session_id not in _closing_tasks:
        # Recovery: the in-process closing task was lost (restart). Closing has
        # no spoken content at P0, so completing it here is the same transition.
        await _finish_closing(d, session_id)
        meta, state = await _load(d.store, session_id)
    opening = d.coordinator.opening_media(session_id) if d.coordinator else None
    if opening is not None:
        # Backend-observed avatar playback (cloud_lemonslice only); null elsewhere.
        info_fn = getattr(d.backend, "playback_info", None)
        info = info_fn(session_id, opening["media_utterance_id"]) if info_fn else None
        opening = {
            **opening,
            "playback_started_at": _rfc3339((info or {}).get("playback_started_at")),
        }
    return {
        "state": state.model_dump(mode="json"),
        "capabilities": Capabilities.for_session(meta).model_dump(),
        "opening_media": opening,
    }


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
        staged = await _stage_usage(d, meta, state, updated, evidence)
        try:
            await _save(d.store, session_id, meta, fence)
        except BaseException as exc:
            await _settle_usage(d, staged, saved=False, failure=exc)
            raise
        await _settle_usage(d, staged, saved=True)
    return {"state": updated.model_dump(mode="json")}


class BudgetLeaseRequest(BaseModel):
    identity: ExecutionIdentity
    lease_id: str = Field(min_length=1, max_length=255)
    sequence: int = Field(ge=1)
    expires_at: str = Field(min_length=1, max_length=64)


@router.post("/sessions/{session_id}/execution/budget-lease")
async def push_budget_lease(
    session_id: str, body: BudgetLeaseRequest, request: Request, _: None = Depends(admin_auth)
) -> dict[str, Any]:
    """P0-FB-018 S6: store the backend-issued lease. Inert (409) unless enforcement is on."""
    if not budget_lease.is_active():
        raise HTTPException(status_code=409, detail={"code": "budget_lease_not_enabled"})
    d = container_from_request(request)
    async with _locked(d.store, session_id) as fence:
        meta, state = await _load(d.store, session_id)
        try:
            lease, applied = budget_lease.accept_lease(
                meta,
                state,
                body.identity,
                body.lease_id,
                body.sequence,
                body.expires_at,
                datetime.now(timezone.utc),
                budget_lease.LeaseSettings.from_env(),
            )
        except budget_lease.LeaseRejection as exc:
            raise HTTPException(status_code=exc.status, detail={"code": exc.code}) from exc
        if applied:
            await _save(d.store, session_id, meta, fence)
            d.approved_speech.set_lease_expiry(
                session_id, budget_lease.parse_expiry(lease["expires_at"])
            )
    enforcer = getattr(d, "budget_lease_enforcer", None)
    if applied and enforcer is not None:
        enforcer.track(session_id)
    return {"lease": lease, "applied": applied}


async def _checked_reason(d: Any, request: Request, session_id: str, command: Any) -> str | None:
    """FLAG-018-1: ``reason_code`` only on end/emergency_end, only from system:live-credits
    over the admin plane, and only ``entitlement_exhausted``. Anything else is a typed
    4xx and an audited rejection. Absent reason_code is exactly today's behavior."""
    reason = getattr(command, "reason_code", None)
    if reason is None:
        return None
    why = None
    if command.command not in ("end", "emergency_end"):
        why, status = "reason_code_not_allowed", 422
    else:
        try:
            await admin_auth(request)
            admin = True
        except HTTPException:
            admin = False
        if command.actor_id != budget_lease.SYSTEM_ACTOR or not admin:
            why, status = "reason_code_forbidden", 403
        elif reason != budget_lease.EXHAUSTED:
            why, status = "invalid_reason_code", 422
    if why is not None:
        await budget_lease.audit_reason_code_rejected(
            d, session_id, command.actor_id, command.command, why
        )
        raise HTTPException(status_code=status, detail={"code": why})
    return reason


@router.post("/sessions/{session_id}/execution/commands")
async def request_execution_command(
    session_id: str,
    wire: RescueCommandRequest,
    request: Request,
    _: None = Depends(viewer_or_admin_auth),
) -> dict[str, Any]:
    # Viewer OR admin token. Admin without reason_code behaves exactly like viewer;
    # reason_code is accepted only with the admin token + system actor (_checked_reason).
    d = container_from_request(request)
    end_reason = await _checked_reason(d, request, session_id, wire)
    command = CommandRequest(**wire.model_dump(exclude={"reason_code"}))
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
            if original.end_reason != end_reason:
                raise HTTPException(status_code=409, detail={"code": "duplicate_command_conflict"})
            if state.phase == "closing" and session_id not in _closing_tasks:
                # A transient fault (or a restart) stranded the completion: restart it.
                _start_closing(d, session_id)
            return {"outcome": original.model_dump(mode="json"), "replayed": True}
        reason = command_rejection(state, command, Capabilities.for_session(meta))
        if reason is None:
            reason = rescue_rejection(state, command.command)
        if reason is None and command.command in ("hold", "resume"):
            if not d.coordinator or not d.coordinator.has(session_id):
                reason = "runtime_not_ready"
        now = datetime.now(timezone.utc)
        staged: list = []
        if reason is not None:
            outcome = CommandOutcome(
                **command.model_dump(),
                status="rejected",
                reason_code=reason,
                result_at=now,
                end_reason=end_reason,
            )
        else:
            prior_state = state
            state = apply_rescue(state, command.command, command.command_id, now)
            outcome = CommandOutcome(
                **command.model_dump(),
                status="applied",
                reason_code="applied_command",
                result_at=now,
                sequence=state.sequence,
                effect=RESCUE_EFFECTS[command.command],
                held=state.hold.held,
                end_reason=end_reason,
            )
            meta["execution_contract"] = state.model_dump(mode="json")
            if command.command == "emergency_end":
                _defer_usage(d, meta, prior_state, state, outcome, session_id)
            else:
                staged = await _stage_usage(d, meta, prior_state, state, outcome)
        meta.setdefault("execution_command_outcomes", {})[command.command_id] = outcome.model_dump(
            mode="json"
        )
        # Persist before the effect: a lost fence answers 503 with no effect.
        # Hold sets its start fence first so no turn can begin between the
        # persisted `held` and the block; a failed save restores it.
        hold_fenced = outcome.status == "applied" and command.command == "hold"
        if hold_fenced:
            prior_block = d.approved_speech.held_reason(session_id)  # Hold-owned state only
            d.approved_speech.block(session_id, "held")
        try:
            await _save(d.store, session_id, meta, fence)
        except BaseException as exc:
            if hold_fenced:
                d.approved_speech.block(session_id, prior_block)
            await _settle_usage(d, staged, saved=False, failure=exc)
            raise
        await _settle_usage(d, staged, saved=True)
        if outcome.status == "applied":
            await _apply_rescue_effect(d, session_id, command.command)
    if outcome.status == "applied":
        if d.hub is not None:
            await d.hub.emit(
                session_id,
                {"type": "execution.command_result", "outcome": outcome.model_dump(mode="json")},
            )
        if command.command == "end":
            _start_closing(d, session_id)
        if command.command == "emergency_end" and UNSTAGED_KEY in meta:
            await _drain_soon(d, session_id)
    return {"outcome": outcome.model_dump(mode="json"), "replayed": False}


def _start_closing(d: Any, session_id: str) -> None:
    task = asyncio.create_task(_complete_closing(d, session_id))
    _closing_tasks[session_id] = task
    task.add_done_callback(
        lambda t: (
            _closing_tasks.pop(session_id, None) if _closing_tasks.get(session_id) is t else None
        )
    )


async def use_execution_command(d: Any, session_id: str) -> bool:
    """Legacy interrupt is refused only on a rescue-enabled (Facebook P0) session.

    TikTok/legacy sessions and sessions without the API marker keep the
    unchanged legacy control even when the Runtime switch is on.
    """
    meta = await d.store.get(session_id)
    return bool(
        meta
        and meta.get("execution_contract")
        and Capabilities.for_session(meta).supports("command.interrupt")
    )


async def hard_cancel(d: Any, session_id: str) -> None:
    """Hard-cancel playback and fence all older output (Interrupt/End/Emergency).

    The approved-speech epoch and coordinator generation are bumped before any
    provider call, so a late TTS/provider completion is refused at dispatch.
    ``backend.interrupt`` also clears provider-managed avatar buffers.
    """
    d.approved_speech.cancel(session_id)
    if d.coordinator is not None and d.coordinator.has(session_id):
        await d.coordinator.interrupt(session_id)
        return
    entry = d.orchestrators.get(session_id)
    if entry is not None:
        await entry["orchestrator"].cancel(session_id)
    await asyncio.to_thread(d.backend.interrupt, session_id)


async def _apply_rescue_effect(d: Any, session_id: str, command: str) -> None:
    speech = d.approved_speech
    if command == "hold":
        # Safe boundary: the current utterance finishes; nothing new starts.
        speech.block(session_id, "held")
        return
    if command == "resume":
        speech.block(session_id, None)
        # Same event-loop step as the unblock: expiry runs before any playback.
        d.coordinator.resume(session_id)
        return
    if command == "end":
        speech.block(session_id, "closing")
    elif command == "emergency_end":
        speech.block(session_id, "ending")
    try:
        await hard_cancel(d, session_id)
    except Exception:
        # The dispatch fence is already bumped; only the provider flush failed.
        logger.warning(
            "rescue %s provider flush failed session=%s", command, session_id, exc_info=True
        )


async def _finish_closing(d: Any, session_id: str) -> None:
    """One best-effort completion attempt (the GET recovery path)."""
    try:
        await _finish_closing_once(d, session_id)
    except Exception:
        logger.exception("closing completion failed session=%s", session_id)


async def _finish_closing_once(d: Any, session_id: str) -> None:
    """Report ``closing -> ending`` once approved closing content is done. Raises on failure.

    ponytail: the approved envelope has no closing artifact yet, so closing
    speaks nothing and completes at once. Play approved closing here first
    when authoring provides one.
    """
    async with _locked(d.store, session_id) as fence:
        meta = await d.store.get(session_id)
        if not meta or not meta.get("execution_contract"):
            return
        meta = copy.deepcopy(meta)  # a failed attempt must not leak into a shared store object
        state = ExecutionState.model_validate(meta["execution_contract"])
        if state.phase != "closing":
            return
        closing_evidence = Evidence(
            **state.model_dump(include=set(ExecutionIdentity.model_fields)),
            sequence=state.sequence + 1,
            kind="phase_changed",
            phase="ending",
            occurred_at=datetime.now(timezone.utc),
        )
        prior_state = state
        state = apply_evidence(state, closing_evidence)
        meta["execution_contract"] = state.model_dump(mode="json")
        staged = await _stage_usage(d, meta, prior_state, state, closing_evidence)
        try:
            await _save(d.store, session_id, meta, fence)
        except BaseException as exc:
            await _settle_usage(d, staged, saved=False, failure=exc)
            raise
        await _settle_usage(d, staged, saved=True)
    d.approved_speech.block(session_id, "ending")
    if d.hub is not None:
        await d.hub.emit(
            session_id,
            {"type": "execution.phase_changed", "phase": "ending", "sequence": state.sequence},
        )


CLOSING_ATTEMPTS = 12  # ~3 min of backoff: outlasts the sweeper's resolve window
CLOSING_BACKOFF = 0.5  # seconds, doubled per attempt, capped at 30


async def _complete_closing(d: Any, session_id: str) -> None:
    """Retry ``closing -> ending`` with backoff; never leave End stranded by a transient fault.

    After ``CLOSING_ATTEMPTS`` failures it logs an ERROR and stops; the session stays
    ``closing`` and is recovered by the next GET /execution or a replayed End.
    """
    for attempt in range(CLOSING_ATTEMPTS):
        try:
            await _finish_closing_once(d, session_id)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "closing completion attempt %d/%d failed session=%s",
                attempt + 1,
                CLOSING_ATTEMPTS,
                session_id,
            )
            await asyncio.sleep(min(30.0, CLOSING_BACKOFF * (2**attempt)))
    logger.error(
        "closing completion parked after %d attempts session=%s", CLOSING_ATTEMPTS, session_id
    )


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
    failures = getattr(d, "runtime_failures", None)
    if failures is not None:
        failures.register(session_id, meta)
    # Existing stop() removes the coordinator; activation checks it again.
    try:
        d.coordinator.activate_approved(session_id, opening, envelope)
    except (SpeechRejected, KeyError):
        return {"outcome": outcome.model_dump(mode="json"), "replayed": False}
    outcome = result("applied", "opening_scheduled", opening)
    outcomes[command.command_id] = outcome.model_dump(mode="json")
    await _save(d.store, session_id, meta, fence)
    return {"outcome": outcome.model_dump(mode="json"), "replayed": False}
