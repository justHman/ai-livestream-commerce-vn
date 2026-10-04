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
import copy
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
# Durable completion marker, written in the SAME atomic save as `failed`: "pending" until
# cleanup and the terminal record are done (the stop path then deletes the session meta).
TERMINATION_KEY = "execution_lease_termination"
UNSTAGED_KEY = "usage_terminal_unstaged"
# TERMINATION_KEY values: "pending" (cleanup / 019 record owed) -> "settling" (cleanup and the
# 019 record are DONE; the session meta is kept only for the unstaged usage fact).
UNSTAGEABLE_AFTER = 5  # permanent (422) staging failures before the fact is parked for an operator
SCAN_EVERY = 30  # sweeps between meta scans for stranded lease work
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


async def termination_pending(d: Any, session_id: str) -> bool:
    """True while a lease-expiry failure still owes cleanup / its terminal record."""
    meta = await d.store.get(session_id)
    return bool(meta and meta.get(TERMINATION_KEY) == "pending")


async def keep_for_unstaged(d: Any, session_id: str, fence: Any = None) -> bool:
    """After cleanup + the 019 record: try the unstaged fact once. True => KEEP the meta.

    Never raises (except cancellation): a staging failure must not block media cleanup or the
    terminal record, which already happened. A permanent (422) failure repeated
    ``UNSTAGEABLE_AFTER`` times parks the fact as ``unstageable`` (ERROR, health counter) and
    the meta is kept for an operator.
    """
    from backend.api.v1.execution import _save

    meta = await d.store.get(session_id)
    fact = (meta or {}).get(UNSTAGED_KEY)
    if not fact:
        return False
    if fact.get("unstageable"):
        return True
    try:
        await settle_unstaged(d, session_id, fence)
        return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        meta = copy.deepcopy(await d.store.get(session_id) or {})
        fact = meta.get(UNSTAGED_KEY)
        if not fact:
            return False
        meta[TERMINATION_KEY] = "settling"
        if getattr(exc, "status_code", None) == 422:
            fact["attempts"] = int(fact.get("attempts", 0)) + 1
            if fact["attempts"] >= UNSTAGEABLE_AFTER:
                fact["unstageable"] = True
                logger.error(
                    "usage terminal fact UNSTAGEABLE, parked for an operator session=%s",
                    session_id,
                )
        logger.error("usage terminal fact still unstaged session=%s", session_id, exc_info=True)
        try:
            await _save(d.store, session_id, meta, fence)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("could not persist unstaged marker session=%s", session_id, exc_info=True)
        return True


async def settle_unstaged(d: Any, session_id: str, fence: Any = None) -> None:
    """Stage the terminal usage fact that could not be staged when the lease failed.

    Caller holds the session lock. Idempotent by the fact's semantic event id (normal
    017 stage -> save -> commit path). While it cannot be staged this raises 503 so the
    session meta (the only record of the fact) is NOT deleted; the watcher retries.
    """
    from backend.api.v1.execution import _save, _settle_usage, _stage_usage

    meta = await d.store.get(session_id)
    fact = (meta or {}).get(UNSTAGED_KEY)
    if not fact:
        return
    meta = copy.deepcopy(meta)
    meta.pop(UNSTAGED_KEY, None)
    if getattr(d, "usage_evidence", None) is not None:
        prior = ExecutionState.model_validate(fact["prior"])
        evidence = Evidence.model_validate(fact["evidence"])
        updated = ExecutionState.model_validate(meta["execution_contract"])
        staged = await _stage_usage(d, meta, prior, updated, evidence)  # raises 503 / 422
        try:
            await _save(d.store, session_id, meta, fence)
        except BaseException as exc:
            await _settle_usage(d, staged, saved=False, failure=exc)
            raise
        await _settle_usage(d, staged, saved=True)
    else:  # the service was switched off since: nothing can be staged any more
        await _save(d.store, session_id, meta, fence)


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
        # Sessions whose terminal usage fact is still unstaged (health / backlog counter),
        # with the per-session retry backoff (attempts, not-before).
        self.unstaged_pending: dict[str, tuple[int, datetime]] = {}
        self.unstageable: set[str] = set()
        self._sweeps = 0
        # The speech gate compares THIS clock with the expiry at check time.
        container.approved_speech.lease_clock = clock
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

    async def rehydrate(self) -> None:
        """At startup, before traffic: load every persisted lease expiry into the speech gate."""
        ids = self._candidates()
        if asyncio.iscoroutine(ids):
            ids = await ids
        ids = set(ids) | set(await self._stored_ids())
        for session_id in ids:
            try:
                meta = await self._d.store.get(session_id)
                self._sync_expiry(session_id, meta)
                if meta and (
                    LEASE_KEY in meta
                    or meta.get(TERMINATION_KEY) in ("pending", "settling")
                    or meta.get(UNSTAGED_KEY)
                ):
                    self.tracked.add(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("lease rehydrate failed session=%s", session_id)

    async def _stored_ids(self) -> list[str]:
        lister = getattr(self._d.store, "list_session_ids", None)
        if lister is None:
            return []
        try:
            return list(await lister())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("lease scan: session ids unreadable", exc_info=True)
            return []

    async def _scan_pending(self) -> None:
        """Re-track lease work that registry discovery cannot see (e.g. an execution that
        already has a 019 terminal record is excluded from list_unterminated_executions)."""
        for session_id in await self._stored_ids():
            try:
                meta = await self._d.store.get(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("lease pending scan deferred error_type=%s", type(exc).__name__)
                continue
            if meta and (
                meta.get(TERMINATION_KEY) in ("pending", "settling") or meta.get(UNSTAGED_KEY)
            ):
                self.tracked.add(session_id)

    def _sync_expiry(self, session_id: str, meta: Mapping[str, Any] | None) -> None:
        expiry = None
        lease = (meta or {}).get(LEASE_KEY)
        if lease and meta.get("execution_contract"):
            try:
                expiry = parse_expiry(lease["expires_at"])
            except (LeaseRejection, KeyError, TypeError):
                expiry = None
        self._d.approved_speech.set_lease_expiry(session_id, expiry)

    async def sweep(self) -> list[str]:
        """Enforce expiry once. Returns the sessions ended by this pass."""
        self._sweeps += 1
        if self._sweeps % SCAN_EVERY == 0:
            await self._scan_pending()
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
        d = self._d
        meta = await d.store.get(session_id)
        if not meta or not meta.get("execution_contract"):
            self._release(session_id)
            return False
        state = ExecutionState.model_validate(meta["execution_contract"])
        if state.phase == "failed" and meta.get(TERMINATION_KEY) == "pending":
            # Failed by lease expiry but cleanup / terminal record not done yet: retry.
            return await self._complete(session_id)
        if state.phase == "failed" and meta.get(TERMINATION_KEY) == "settling":
            return await self._settle(session_id)  # only the usage fact is still owed
        if LEASE_KEY not in meta or state.phase in ("ended", "failed"):
            self._release(session_id)  # no lease, or terminal: drop tracking AND the expiry
            return False
        now = self._clock()
        self._sync_expiry(session_id, meta)  # renewal / rehydrate; the gate itself is clock-based
        if state.phase not in _ENFORCED_PHASES:
            return False  # pre-live / closing: keep tracking and the expiry fence
        if not lease_expired(meta, now):
            self._gate_started.pop(session_id, None)  # renewed inside the wait window
            return False
        started = self._gate_started.setdefault(session_id, now)
        timed_out = (now - started).total_seconds() >= self._settings.safe_boundary_wait
        if self._is_speaking(session_id) and not timed_out:
            return False
        return await self._end_control_lost(session_id, hard=self._is_speaking(session_id))

    def _release(self, session_id: str) -> None:
        self._gate_started.pop(session_id, None)
        self.tracked.discard(session_id)
        self._d.approved_speech.set_lease_expiry(session_id, None)

    async def _end_control_lost(self, session_id: str, *, hard: bool) -> bool:
        """failed(execution_failed, control_lost), idempotent under the session lock."""
        from backend.api.v1.execution import _load, _locked, _save, hard_cancel

        d, speech = self._d, self._d.approved_speech
        async with _locked(d.store, session_id) as fence:
            try:
                meta, state = await _load(d.store, session_id)
            except Exception:
                return False
            meta = copy.deepcopy(meta)  # a failed attempt must not leak into a shared store object
            now = self._clock()
            # Re-check under the lock: renewed, already ending or already terminal => no-op.
            if state.phase not in _ENFORCED_PHASES or not lease_expired(meta, now):
                return False
            prior_state = state
            forced = Evidence(
                **state.model_dump(include=set(ExecutionIdentity.model_fields)),
                sequence=state.sequence + 1,
                kind="terminal",
                phase="failed",
                reason_code="execution_failed",
                occurred_at=now,
            )
            state = apply_evidence(state, forced)
            meta["execution_contract"] = state.model_dump(mode="json")
            meta[FAILURE_KEY] = "control_lost"
            meta[TERMINATION_KEY] = "pending"
            # 017: a SAFETY stop never depends on a database call. The terminal fact is only
            # RECORDED here, in the same atomic save as the state; staging runs after the stop
            # took effect (bounded, see _settle_inline) and is retried by the completion.
            if getattr(d, "usage_evidence", None) is not None:
                meta[UNSTAGED_KEY] = {
                    "prior": prior_state.model_dump(mode="json"),
                    "evidence": forced.model_dump(mode="json"),
                }
            await _save(d.store, session_id, meta, fence)
        speech.block(session_id, "ending")
        if hard:
            try:
                await hard_cancel(d, session_id)
            except Exception:
                logger.warning("lease expiry provider flush failed session=%s", session_id)
        if UNSTAGED_KEY in meta:
            await self._settle_inline(session_id, now)
        logger.warning(
            "audit_event=budget_lease_expired failure_class=control_lost session=%s", session_id
        )
        if d.hub is not None:
            await d.hub.emit(
                session_id,
                {"type": "execution.phase_changed", "phase": "failed", "sequence": state.sequence},
            )
        await self._complete(session_id)
        return True

    async def _settle_inline(self, session_id: str, now: datetime) -> None:
        """Bounded first attempt to stage the terminal fact, AFTER the stop took effect."""
        from backend.api.v1.execution import DEFER_DRAIN_BUDGET, _locked, run_with_deadline

        async def attempt() -> None:
            async with _locked(self._d.store, session_id) as fence:
                try:
                    await settle_unstaged(self._d, session_id, fence)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error(
                        "lease expiry: usage terminal evidence NOT staged session=%s; the "
                        "completion retries it",
                        session_id,
                        exc_info=True,
                    )

        # A hard deadline: the caller never awaits the attempt's cancellation.
        await run_with_deadline(attempt(), DEFER_DRAIN_BUDGET)
        meta = await self._d.store.get(session_id)
        if meta is not None and meta.get(UNSTAGED_KEY):
            self.unstaged_pending[session_id] = (0, now)

    async def _complete(self, session_id: str) -> bool:
        """Cleanup + durable terminal record through the SAME path as POST /stop.

        Idempotent: success deletes the session meta (nothing left to retry); a failure
        leaves the ``pending`` marker so the next sweep retries. Never raises.
        """
        from backend.api.v1.execution import DEFER_DRAIN_BUDGET, run_with_deadline
        from backend.api.v1.sessions import stop_session_internal

        now = self._clock()
        attempts, not_before = self.unstaged_pending.get(session_id, (0, now))
        if now < not_before:
            return False  # backing off a failing staging; the marker stays durable
        completed = False
        failure = None

        async def attempt() -> None:
            nonlocal completed, failure
            try:
                await stop_session_internal(self._d, session_id)
                completed = True
            except Exception as exc:
                failure = exc

        try:
            await run_with_deadline(attempt(), DEFER_DRAIN_BUDGET)
            if failure is not None:
                raise failure
            if not completed:
                # Completion can encounter the same hanging DB as the first staging
                # attempt. Keep its durable pending marker and advance to other leases.
                self.tracked.add(session_id)
                self.unstaged_pending[session_id] = (
                    attempts + 1,
                    now + timedelta(seconds=min(30.0, 0.5 * (2 ** min(attempts + 1, 6)))),
                )
                return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "lease termination completion failed, will retry session=%s unstaged_pending=%d",
                session_id,
                len(self.unstaged_pending),
                exc_info=True,
            )
            self.tracked.add(session_id)
            return False
        meta = await self._d.store.get(session_id)
        if meta is not None and meta.get(UNSTAGED_KEY):
            self._note_unstaged(session_id, meta, now)  # cleanup + 019 record are done
            return True
        self.unstaged_pending.pop(session_id, None)
        self.unstageable.discard(session_id)
        self._release(session_id)
        return True

    def _note_unstaged(self, session_id: str, meta: Mapping[str, Any], now: datetime) -> None:
        """Backlog counter + backoff for a terminal fact that is still unstaged."""
        self.tracked.add(session_id)
        if meta[UNSTAGED_KEY].get("unstageable"):
            self.unstaged_pending.pop(session_id, None)
            self.unstageable.add(session_id)
            return
        attempts = self.unstaged_pending.get(session_id, (0, now))[0] + 1
        self.unstaged_pending[session_id] = (
            attempts,
            now + timedelta(seconds=min(30.0, 0.5 * (2**attempts))),
        )

    async def _settle(self, session_id: str) -> bool:
        """Cleanup and the 019 record are done: retry ONLY the unstaged usage fact."""
        from backend.api.v1.execution import DEFER_DRAIN_BUDGET, _locked, run_with_deadline
        from backend.api.v1.sessions import delete_session_meta

        now = self._clock()
        if now < self.unstaged_pending.get(session_id, (0, now))[1]:
            return False
        settled = False

        async def attempt() -> None:
            nonlocal settled
            # The detached attempt owns its lock, including release/cancellation.
            async with _locked(self._d.store, session_id) as fence:
                if await keep_for_unstaged(self._d, session_id, fence):
                    return
                await delete_session_meta(self._d, session_id, fence)
                settled = True

        await run_with_deadline(attempt(), DEFER_DRAIN_BUDGET)
        if not settled:
            meta = await self._d.store.get(session_id)
            if meta is not None and meta.get(UNSTAGED_KEY):
                self._note_unstaged(session_id, meta, now)
            return False
        self.unstaged_pending.pop(session_id, None)
        self.unstageable.discard(session_id)
        self._release(session_id)
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
