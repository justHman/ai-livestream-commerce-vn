"""Default-off P0-FB-020 R1: bounded local detection and retained terminal work.

No capability is advertised. A local stop fences speech before durable I/O;
FAILED is exposed only after the atomic execution-state save. The existing
019/017 completion path owns cleanup, terminal persistence and usage retries.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.application import budget_lease as bl
from backend.application.execution_contract import (
    Evidence,
    ExecutionIdentity,
    ExecutionState,
    apply_evidence,
)

logger = logging.getLogger(__name__)
ENV_ENABLED = "RUNTIME_FAILURE_DETECTION_ENABLED"
ORIGIN_KEY = "execution_runtime_failure"


@dataclass(frozen=True)
class FailureSettings:
    # Temporary implementation defaults; measure before production acceptance.
    enabled: bool = False
    unhealthy_ticks: int = 5
    terminal_ticks: int = 20
    attempt_timeout: float = 3.0
    poll: float = 1.0

    @classmethod
    def from_env(cls) -> FailureSettings:
        enabled = os.getenv(ENV_ENABLED, "0").strip().lower() in ("1", "true", "yes", "on")
        if not enabled:
            return cls()
        unhealthy = int(os.getenv("RUNTIME_UNHEALTHY_TICKS", "5"))
        terminal = int(os.getenv("RUNTIME_TERMINAL_ERROR_TICKS", "20"))
        if unhealthy < 1 or terminal <= unhealthy:
            raise ValueError("invalid runtime failure thresholds")
        return cls(enabled=enabled, unhealthy_ticks=unhealthy, terminal_ticks=terminal)


def failure_class(error: Exception) -> str:
    """Provider text is never classification input or a diagnostic value."""
    current: BaseException | None = error
    for _ in range(5):
        if current is None:
            break
        module = type(current).__module__
        if any(part in module for part in ("avatar", "publishing", "livekit")):
            return "media_failed"
        if (
            module.endswith("script_authoring.approved_speech")
            or ".script_authoring.gate." in module
            or (
                module.endswith("script_authoring.generation.batch")
                and type(current).__name__ == "ContentFailure"
            )
        ):
            return "safety_failed"
        current = current.__cause__
    return "runtime_error"


@dataclass
class Pending:
    identity: dict[str, Any]
    kind: str
    occurred_at: datetime
    healthy: bool | None = None
    fault: str | None = None
    owner: Any = None
    token: str | None = None
    attempts: int = 0
    next_attempt: float = 0


class RuntimeFailures:
    def __init__(self, container: Any, settings: FailureSettings):
        self.d, self.settings = container, settings
        self.identities: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, Pending] = {}
        self.consecutive: dict[str, int] = {}
        self.flush_tasks: set[asyncio.Task] = set()
        self.discovery_backoff: dict[str, tuple[int, float]] = {}
        self.counters = {
            "tick_errors": 0,
            "provider_failures": 0,
            "terminal_tick_failures": 0,
            "failures_saved": 0,
            "attempt_errors": 0,
            "stale_reports": 0,
        }
        # Reuse the complete/settle helpers only: this does not activate a lease,
        # change admission expiries or advertise budget.lease.v1.
        self.completion = bl.BudgetLeaseEnforcer(container, bl.LeaseSettings())

    def register(self, session_id: str, meta: dict[str, Any]) -> None:
        raw = meta.get("execution_contract")
        if not raw:
            return
        state = ExecutionState.model_validate(raw)
        identity = state.model_dump(include=set(ExecutionIdentity.model_fields))
        if self.identities.get(session_id) != identity:
            self.pending.pop(session_id, None)
            self.consecutive.pop(session_id, None)
            self.d.approved_speech.set_runtime_failed(session_id, False)
        self.identities[session_id] = identity

    def tick(self, session_id: str, error: Exception | None) -> None:
        if session_id not in self.identities:
            return  # legacy executions are inert
        pending = self.pending.get(session_id)
        if pending and pending.kind == "terminal":
            return
        previous = self.consecutive.get(session_id, 0)
        if error is None:
            self.consecutive[session_id] = 0
            if previous >= self.settings.unhealthy_ticks:
                self._health(session_id, True)
            return
        self.counters["tick_errors"] += 1
        count = previous + 1
        self.consecutive[session_id] = count
        if count >= self.settings.terminal_ticks:
            self.fail(session_id, error, from_tick=True)
        elif count == self.settings.unhealthy_ticks:
            self._health(session_id, False)

    def _health(self, session_id: str, healthy: bool) -> None:
        coordinator = self.d.coordinator
        owner = coordinator._runtime._sessions.get(session_id)
        if owner is None:
            return
        self.pending[session_id] = Pending(
            self.identities[session_id],
            "health",
            datetime.now(timezone.utc),
            healthy=healthy,
            owner=owner,
            token=owner.generation_token,
        )

    def fail(
        self,
        session_id: str,
        error: Exception,
        token: str | None = None,
        *,
        from_tick: bool = False,
    ) -> None:
        if session_id not in self.identities:
            return
        old = self.pending.get(session_id)
        if old and old.kind == "terminal":
            return
        coordinator = self.d.coordinator
        owner = coordinator._runtime._sessions.get(session_id)
        if owner is None or (token and owner.generation_token != token):
            self.counters["stale_reports"] += 1
            return
        self.counters["terminal_tick_failures" if from_tick else "provider_failures"] += 1
        # Synchronous fence: neither Redis nor Postgres/provider cancellation
        # can delay refusal of late/in-flight output or new decisions.
        self.d.approved_speech.block(session_id, "ending")
        self.d.approved_speech.set_runtime_failed(session_id, True)
        self.d.approved_speech.cancel(session_id)
        coordinator._activated.discard(session_id)
        coordinator._invalidate_queued(session_id, "runtime_failure")
        coordinator._runtime.invalidate_generation(session_id)
        self.pending[session_id] = Pending(
            self.identities[session_id],
            "terminal",
            datetime.now(timezone.utc),
            fault="runtime_error" if from_tick else failure_class(error),
            owner=owner,
            token=owner.generation_token,
        )
        logger.error(
            "audit=runtime_failure session=%s class=%s", session_id, self.pending[session_id].fault
        )
        task = asyncio.create_task(self._flush(session_id, self.pending[session_id]))
        self.flush_tasks.add(task)
        task.add_done_callback(self.flush_tasks.discard)

    async def _flush(self, session_id: str, pending: Pending) -> None:
        """Flush provider buffers immediately, independently of durable I/O."""
        from backend.api.v1.execution import run_with_deadline

        if not self._current(session_id, pending):
            return

        async def interrupt():
            try:
                await asyncio.to_thread(self.d.backend.interrupt, session_id)
            except Exception as exc:
                logger.warning("runtime failure flush retry class=%s", type(exc).__name__)

        async def cancel():
            entry = self.d.orchestrators.get(session_id)
            if entry is not None:
                try:
                    await entry["orchestrator"].cancel(session_id)
                except Exception as exc:
                    logger.warning("runtime failure cancel retry class=%s", type(exc).__name__)

        await asyncio.gather(
            run_with_deadline(asyncio.create_task(interrupt()), self.settings.attempt_timeout),
            run_with_deadline(asyncio.create_task(cancel()), self.settings.attempt_timeout),
        )

    def _current(self, session_id: str, pending: Pending) -> bool:
        owner = self.d.coordinator._runtime._sessions.get(session_id)
        # A validated fatal fault is execution-scoped: subsequent Interrupt or
        # config revisions in that same execution cannot erase it. Health
        # observations still refer only to the revision actually tested.
        return owner is pending.owner and (
            pending.kind == "terminal" or owner.generation_token == pending.token
        )

    async def _record(self, session_id: str, pending: Pending) -> bool:
        from backend.api.v1.execution import _load, _locked, _save, _stage_usage, _settle_usage

        async with _locked(self.d.store, session_id) as fence:
            meta, state = await _load(self.d.store, session_id)
            if (
                state.model_dump(include=set(ExecutionIdentity.model_fields)) != pending.identity
                or not self._current(session_id, pending)
                or state.phase in ("ended", "failed")
            ):
                self.counters["stale_reports"] += 1
                return True
            meta = copy.deepcopy(meta)
            evidence = Evidence(
                **pending.identity,
                sequence=state.sequence + 1,
                kind=pending.kind,
                phase="failed" if pending.kind == "terminal" else state.phase,
                reason_code="execution_failed" if pending.kind == "terminal" else None,
                healthy=pending.healthy,
                occurred_at=pending.occurred_at,
            )
            updated = apply_evidence(state, evidence)
            meta["execution_contract"] = updated.model_dump(mode="json")
            staged = []
            if pending.kind == "terminal":
                meta[bl.FAILURE_KEY] = pending.fault
                # Shared safety-stop completion marker has a historical lease
                # name. Its existing pending/settling meaning is unchanged.
                meta[bl.TERMINATION_KEY] = "pending"
                meta[ORIGIN_KEY] = True
                if getattr(self.d, "usage_evidence", None) is not None:
                    from backend.api.v1.execution import _defer_usage

                    _defer_usage(self.d, meta, state, updated, evidence, session_id)
            else:
                staged = await _stage_usage(self.d, meta, state, updated, evidence)
            try:
                await _save(self.d.store, session_id, meta, fence)
            except BaseException as exc:
                await _settle_usage(self.d, staged, saved=False, failure=exc)
                raise
            await _settle_usage(self.d, staged, saved=True)
        if pending.kind == "terminal":
            self.counters["failures_saved"] += 1
            self.completion.tracked.add(session_id)
        return True

    async def sweep(self) -> None:
        from backend.api.v1.execution import run_with_deadline

        now = asyncio.get_running_loop().time()
        for session_id, pending in list(self.pending.items()):
            if now < pending.next_attempt:
                continue
            result = {"completed": False}

            async def attempt(sid=session_id, item=pending, outcome=result):
                try:
                    outcome["completed"] = await self._record(sid, item)
                except Exception as exc:
                    logger.warning(
                        "runtime failure evidence retry session=%s class=%s",
                        sid,
                        type(exc).__name__,
                    )

            await run_with_deadline(attempt(), self.settings.attempt_timeout)
            if result["completed"] and self.pending.get(session_id) is pending:
                self.pending.pop(session_id, None)
            elif not result["completed"]:
                self.counters["attempt_errors"] += 1
                pending.attempts += 1
                pending.next_attempt = now + min(300, 2 ** min(pending.attempts, 9))
        # Discovery itself is bounded: an unreadable store must not stall all
        # already tracked completion attempts.
        discovered = {"ids": []}

        async def discover():
            discovered["ids"] = await self.completion._stored_ids()

        await run_with_deadline(discover(), self.settings.attempt_timeout)
        # Restart-safe discovery of R1 terminal saves, including writer crashes.
        for session_id in discovered["ids"]:
            attempts, next_at = self.discovery_backoff.get(session_id, (0, 0))
            if now < next_at:
                continue
            result = {"read": False}

            async def read(sid=session_id, outcome=result):
                try:
                    meta = await self.d.store.get(sid)
                    if meta and meta.get(ORIGIN_KEY):
                        self.completion.tracked.add(sid)
                    outcome["read"] = True
                except Exception as exc:
                    logger.warning("runtime failure discovery retry class=%s", type(exc).__name__)

            await run_with_deadline(read(), self.settings.attempt_timeout)
            if result["read"]:
                self.discovery_backoff.pop(session_id, None)
            else:
                self.counters["attempt_errors"] += 1
                self.discovery_backoff[session_id] = (
                    attempts + 1,
                    now + min(300, 2 ** min(attempts + 1, 9)),
                )
        for session_id in tuple(self.completion.tracked):
            result = {"completed": False}

            async def complete(sid=session_id, outcome=result):
                meta = await self.d.store.get(sid)
                if not meta or not meta.get(ORIGIN_KEY):
                    self.completion.tracked.discard(sid)
                    return
                if meta.get(bl.TERMINATION_KEY) == "settling":
                    outcome["completed"] = await self.completion._settle(sid)
                else:
                    outcome["completed"] = await self.completion._complete(sid)
                if outcome["completed"]:
                    # The 017 sender can retain metadata after cleanup. Do not
                    # rediscover it as cleanup work on every R1 sweep.
                    from backend.api.v1.execution import _locked, _save

                    async with _locked(self.d.store, sid) as fence:
                        retained = await self.d.store.get(sid)
                        if retained and retained.get(ORIGIN_KEY):
                            retained = copy.deepcopy(retained)
                            retained.pop(ORIGIN_KEY, None)
                            await _save(self.d.store, sid, retained, fence)

            try:
                await run_with_deadline(complete(), self.settings.attempt_timeout)
            except Exception as exc:
                logger.warning("runtime failure completion retry class=%s", type(exc).__name__)
            if result["completed"]:
                self.identities.pop(session_id, None)
                self.consecutive.pop(session_id, None)

    def health(self) -> dict[str, int]:
        return {
            **self.counters,
            "evidence_pending": len(self.pending),
            "cleanup_pending": len(self.completion.tracked),
        }

    async def run_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("runtime failure sweep error class=%s", type(exc).__name__)
            await asyncio.sleep(self.settings.poll)
