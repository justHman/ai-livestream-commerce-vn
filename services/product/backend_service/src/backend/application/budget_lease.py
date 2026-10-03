"""P0-FB-018 S6 / C-RESCUE-CMD-001 FLAG-018-1: bounded budget lease, Runtime consumer.

The Runtime never computes credits, balances or billable durations. It stores the
lease the backend pushes (``meta["execution_budget_lease"]``) and enforces only its
expiry, on its own clock. The whole slice is inert unless
``LIVE_CREDIT_LEASE_ENFORCEMENT`` is on: the watcher is not started, the route
answers 409 ``budget_lease_not_enabled`` and ``budget.lease.v1`` is not advertised.

Clock skew (documented, never silent): a lease is accepted only when
``now - skew < expires_at <= now + max_horizon`` on the Runtime clock. A lease that
already expired by more than ``skew`` is 422, and a far-future lease is 422, so a
skewed clock cannot grant unbounded free continuation. Enforcement fires at
``expires_at`` on the Runtime clock, so a Runtime clock that runs behind the backend
continues for at most that offset; the deployment keeps both on NTP.

Expiry without renewal (H6 default): no new turn starts (the 016 start fence, reason
``lease_expired``), the utterance in flight finishes at its safe boundary, then the
execution ends ``failed(execution_failed)`` with ``failure_class=control_lost``. If the
utterance has not finished within ``safe_boundary_wait`` it is hard-cancelled and the
session ends anyway. This is never ``entitlement_exhausted``: entitlement is unknown.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from backend.application.execution_contract import (
    Evidence,
    ExecutionIdentity,
    ExecutionState,
    apply_evidence,
)

logger = logging.getLogger(__name__)

ENV_ENABLED = "LIVE_CREDIT_LEASE_ENFORCEMENT"
ENV_WAIT = "LIVE_CREDIT_SAFE_BOUNDARY_WAIT_SECONDS"
ENV_SKEW = "LIVE_CREDIT_LEASE_CLOCK_SKEW_SECONDS"
ENV_HORIZON = "LIVE_CREDIT_LEASE_MAX_HORIZON_SECONDS"
LEASE_KEY = "execution_budget_lease"
FAILURE_KEY = "execution_failure_class"
GATE_REASON = "lease_expired"
SYSTEM_ACTOR = "system:live-credits"
EXHAUSTED = "entitlement_exhausted"
_TRUE = ("1", "true", "yes", "on")
# Enforced only while the viewer-facing execution runs. Before live nothing is billed
# and a start is the API's decision (it fails closed); closing/ending/terminal already end.
_ENFORCED_PHASES = ("warming", "selling")

# Advertised only while the watcher is wired and enabled (set by the lifespan).
_active = False


def set_active(value: bool) -> None:
    global _active
    _active = bool(value)


def is_active() -> bool:
    return _active


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class LeaseSettings:
    enabled: bool = False
    safe_boundary_wait: float = 30.0
    skew: float = 5.0
    max_horizon: float = 3600.0
    poll: float = 1.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LeaseSettings":
        env = os.environ if env is None else env
        return cls(
            enabled=env.get(ENV_ENABLED, "0").strip().lower() in _TRUE,
            safe_boundary_wait=_env_float(env, ENV_WAIT, 30.0),
            skew=_env_float(env, ENV_SKEW, 5.0),
            max_horizon=_env_float(env, ENV_HORIZON, 3600.0) or 3600.0,
        )


class LeaseRejection(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


def rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_expiry(raw: str) -> datetime:
    """RFC3339 with an explicit offset only (a naive time is ambiguous)."""
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise LeaseRejection(422, "invalid_expires_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LeaseRejection(422, "invalid_expires_at")
    return parsed.astimezone(timezone.utc)


def accept_lease(
    meta: dict[str, Any],
    state: ExecutionState,
    identity: ExecutionIdentity,
    lease_id: str,
    sequence: int,
    expires_at: str,
    now: datetime,
    settings: LeaseSettings,
) -> tuple[dict[str, Any], bool]:
    """Validate one pushed lease; mutate ``meta`` and return ``(lease, applied)``.

    Order matters: identity first (a stale generation is never a no-op), then a lower or
    equal sequence (idempotent replay, even if that old lease has since expired), then
    the expiry window.
    """
    if state.model_dump(include=set(ExecutionIdentity.model_fields)) != identity.model_dump():
        raise LeaseRejection(409, "stale_generation")
    if state.phase in ("ended", "failed"):
        raise LeaseRejection(409, "already_terminal")
    stored = meta.get(LEASE_KEY)
    if stored is not None and sequence <= int(stored["sequence"]):
        return stored, False
    expiry = parse_expiry(expires_at)
    if expiry <= now - timedelta(seconds=settings.skew):
        raise LeaseRejection(422, "invalid_expires_at")
    if expiry > now + timedelta(seconds=settings.max_horizon):
        raise LeaseRejection(422, "invalid_expires_at")
    lease = {
        "lease_id": lease_id,
        "sequence": sequence,
        "expires_at": rfc3339(expiry),
        "received_at": rfc3339(now),
    }
    meta[LEASE_KEY] = lease
    return lease, True


def lease_expired(meta: Mapping[str, Any], now: datetime) -> bool:
    lease = meta.get(LEASE_KEY)
    if not lease:
        return False
    try:
        return parse_expiry(lease["expires_at"]) <= now
    except (LeaseRejection, KeyError, TypeError):
        # An unreadable stored lease is never proof of paid time.
        return True


async def audit_reason_code_rejected(
    d: Any, session_id: str, actor: str, command: str, why: str
) -> None:
    """Structured audit line plus a durable row when Postgres is wired. No secrets."""
    logger.warning(
        "audit_event=reason_code_rejected why=%s command=%s actor=%s session=%s",
        why,
        command,
        actor[:64],
        session_id,
    )
    pg = getattr(d, "pg_store", None)
    if pg is not None and getattr(pg, "enabled", False):
        try:
            await pg.insert_audit_event(
                "command.reason_code_rejected",
                session_id=session_id,
                actor=actor[:64],
                resource=command,
                detail={"why": why},
            )
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_audit_event", session_id
            )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BudgetLeaseEnforcer:
    """Expiry watcher. ``sweep`` is one deterministic pass; ``run_loop`` just repeats it."""

    def __init__(
        self,
        container: Any,
        settings: LeaseSettings,
        *,
        clock: Callable[[], datetime] = _utc_now,
        candidates: Callable[[], Any] | None = None,
        is_speaking: Callable[[str], bool] | None = None,
    ) -> None:
        self._d = container
        self._settings = settings
        self._clock = clock
        self._candidates = candidates or self._default_candidates
        self._is_speaking = is_speaking or (lambda sid: sid in self._d.orchestrators)
        self.tracked: set[str] = set()
        self._gate_started: dict[str, datetime] = {}

    def track(self, session_id: str) -> None:
        self.tracked.add(session_id)

    async def _default_candidates(self) -> set[str]:
        """Re-derivable after a restart: tracked + every session the process still holds."""
        d = self._d
        ids = set(self.tracked) | set(getattr(d, "orchestrators", {}) or {})
        publishers = getattr(d, "livekit_publishers", None)
        ids.update(getattr(publishers, "session_ids", ()) or ())
        pg = getattr(d, "pg_store", None)
        if pg is not None and getattr(pg, "enabled", False):
            try:
                ids.update(i.runtime_session_id for i in await pg.list_unterminated_executions())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("lease candidates unreadable error_type=%s", type(exc).__name__)
        return ids

    async def sweep(self) -> list[str]:
        """Enforce expiry once. Returns the sessions ended by this pass."""
        ids = self._candidates()
        if asyncio.iscoroutine(ids):
            ids = await ids
        ended: list[str] = []
        for session_id in sorted(ids):
            try:
                if await self._check(session_id):
                    ended.append(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("budget lease check failed session=%s", session_id)
        return ended

    async def _check(self, session_id: str) -> bool:
        d, speech = self._d, self._d.approved_speech
        meta = await d.store.get(session_id)
        if not meta or LEASE_KEY not in meta or not meta.get("execution_contract"):
            self._release(session_id)
            return False
        state = ExecutionState.model_validate(meta["execution_contract"])
        if state.phase not in _ENFORCED_PHASES:
            self._release(session_id)
            return False
        now = self._clock()
        if not lease_expired(meta, now):
            if session_id in self._gate_started:  # renewed inside the wait window
                self._release(session_id)
                if speech.blocked(session_id) == GATE_REASON:
                    speech.block(session_id, None)
            return False
        started = self._gate_started.setdefault(session_id, now)
        # Re-assert every pass: a merchant Resume must not reopen the gate.
        if speech.blocked(session_id) not in ("closing", "ending"):
            speech.block(session_id, GATE_REASON)
        timed_out = (now - started).total_seconds() >= self._settings.safe_boundary_wait
        if self._is_speaking(session_id) and not timed_out:
            return False
        return await self._end_control_lost(session_id, hard=self._is_speaking(session_id))

    def _release(self, session_id: str) -> None:
        self._gate_started.pop(session_id, None)
        self.tracked.discard(session_id)

    async def _end_control_lost(self, session_id: str, *, hard: bool) -> bool:
        """failed(execution_failed, control_lost), idempotent under the session lock."""
        from backend.api.v1.execution import _load, _locked, _save, hard_cancel

        d, speech = self._d, self._d.approved_speech
        async with _locked(d.store, session_id) as fence:
            try:
                meta, state = await _load(d.store, session_id)
            except Exception:
                return False
            now = self._clock()
            # Re-check under the lock: renewed, already ending or already terminal => no-op.
            if state.phase not in _ENFORCED_PHASES or not lease_expired(meta, now):
                return False
            state = apply_evidence(
                state,
                Evidence(
                    **state.model_dump(include=set(ExecutionIdentity.model_fields)),
                    sequence=state.sequence + 1,
                    kind="terminal",
                    phase="failed",
                    reason_code="execution_failed",
                    occurred_at=now,
                ),
            )
            meta["execution_contract"] = state.model_dump(mode="json")
            meta[FAILURE_KEY] = "control_lost"
            await _save(d.store, session_id, meta, fence)
        speech.block(session_id, "ending")
        if hard:
            try:
                await hard_cancel(d, session_id)
            except Exception:
                logger.warning("lease expiry provider flush failed session=%s", session_id)
        self._release(session_id)
        logger.warning("audit_event=budget_lease_expired failure_class=control_lost session=%s", session_id)
        if d.hub is not None:
            await d.hub.emit(
                session_id,
                {"type": "execution.phase_changed", "phase": "failed", "sequence": state.sequence},
            )
        return True

    async def run_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("budget lease sweep failed")
            await asyncio.sleep(self._settings.poll)
