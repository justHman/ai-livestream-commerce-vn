"""Always-on LemonSlice cost safety; independent of optional failure monitoring."""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class AvatarLifecycle:
    def __init__(self, container, ready_ttl_s: float = 1200.0, poll_s: float = 5.0):
        self.d = container
        self.ready_ttl_s, self.poll_s = ready_ttl_s, poll_s
        self.tasks: dict[str, asyncio.Task] = {}
        self.pending: dict[str, str] = {}
        self.next_attempt: dict[str, float] = {}

    async def check(self, session_id: str) -> None:
        from backend.api.v1.execution import _locked, _save, _defer_usage
        from backend.api.v1.sessions import stop_session_internal
        from backend.application.execution_contract import (
            Evidence,
            ExecutionIdentity,
            ExecutionState,
            apply_evidence,
        )

        d = self.d
        async with _locked(d.store, session_id) as fence:
            meta = copy.deepcopy(await d.store.get(session_id))
            raw = (meta or {}).get("execution_contract", {})
            phase = raw.get("phase", "ready")
            d.backend.set_session_phase(session_id, phase)
            reason = self.pending.get(session_id) or await asyncio.to_thread(
                d.backend.lifecycle_status, session_id, self.ready_ttl_s
            )
            if not reason:
                return
            self.pending[session_id] = reason
            log.warning("avatar safety stop session=%s reason=%s", session_id, reason)
            if reason == "RUNTIME_READY_TTL_EXPIRED":
                log.warning("Phiên chuẩn bị đã hết thời gian chờ lên sóng; hãy chuẩn bị lại")
            # Fence producers before persistence. Provider I/O runs outside this short lock.
            d.approved_speech.block(session_id, "ending")
            d.approved_speech.cancel(session_id)
            if raw and raw.get("phase") not in {"ended", "failed"}:
                state = ExecutionState.model_validate(raw)
                ev = Evidence(
                    **state.model_dump(include=set(ExecutionIdentity.model_fields)),
                    sequence=state.sequence + 1,
                    kind="terminal",
                    phase="failed",
                    reason_code="execution_failed",
                    occurred_at=datetime.now(timezone.utc),
                )
                updated = apply_evidence(state, ev)
                meta["execution_contract"] = updated.model_dump(mode="json")
                meta["execution_failure_class"] = (
                    "runtime_error" if reason == "RUNTIME_READY_TTL_EXPIRED" else "media_failed"
                )
                meta["avatar_stop_reason"] = reason
                _defer_usage(d, meta, state, updated, ev, session_id)
                try:
                    await _save(d.store, session_id, meta, fence)
                except BaseException:
                    # A failed save must neither leave paid media alive nor delete its meta.
                    # Teardown is owned by the backend even if this task is cancelled.
                    await asyncio.to_thread(d.backend.stop, session_id)
                    raise
        # Existing stop owns reconciliation, publisher, durable terminal/usage and metadata.
        # It acquires a fresh lock after the failure state was committed above.
        await stop_session_internal(d, session_id)
        if reason == "RUNTIME_READY_TTL_EXPIRED":
            d.backend.remember_ready_expiry(session_id)
        self.pending.pop(session_id, None)
        self.next_attempt.pop(session_id, None)

    async def _attempt(self, sid: str) -> None:
        try:
            await self.check(sid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.next_attempt[sid] = asyncio.get_running_loop().time() + 60
            log.error(
                "avatar lifecycle cleanup deferred session=%s error_type=%s",
                sid,
                type(exc).__name__,
            )

    async def run_loop(self) -> None:
        try:
            while True:
                now = asyncio.get_running_loop().time()
                for sid in set(self.d.backend.session_ids()) | set(self.pending):
                    task = self.tasks.get(sid)
                    if (task is None or task.done()) and now >= self.next_attempt.get(sid, 0):
                        self.tasks[sid] = asyncio.create_task(self._attempt(sid))
                # Drop completed task references for sessions whose full cleanup finished.
                self.tasks = {sid: task for sid, task in self.tasks.items() if not task.done()}
                await asyncio.sleep(self.poll_s)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
