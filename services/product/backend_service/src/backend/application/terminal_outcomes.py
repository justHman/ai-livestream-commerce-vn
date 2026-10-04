"""P0-FB-019 / C-TERMINAL-001: durable terminal record and outbox (DISABLED slice).

Everything here is inert unless ``TERMINAL_OUTCOMES_ENABLED`` is set AND a
durable Postgres store, a callback URL and a secret are configured. While it
is inert the ``execution.terminal.v1`` capability is not advertised and the
legacy stop path is byte-for-byte unchanged. Nothing here decides the P0
business mapping; the API owns that (gated step 3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, NamedTuple
from urllib.parse import urlsplit

from backend.application.execution_contract import (
    Cleanup,
    CommandRef,
    EvidenceRefs,
    ExecutionIdentity,
    ExecutionState,
    TerminalRecord,
    validate_terminal_record,
)

logger = logging.getLogger(__name__)

ENV_ENABLED = "TERMINAL_OUTCOMES_ENABLED"
ENV_URL = "TERMINAL_CALLBACK_URL"
ENV_SECRET = "TERMINAL_CALLBACK_SECRET"
PENDING_FLAG = "terminal_persist_pending"
ATTEMPTS_KEY = "terminal_cleanup_attempts"
CLEANUP_MAX_ATTEMPTS = 3

_TRUE = ("1", "true", "yes", "on")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_END_REASON_BY_COMMAND = {"end": "normal_end", "emergency_end": "merchant_emergency_end"}
_END_REASONS = ("normal_end", "merchant_emergency_end", "entitlement_exhausted")
_FAILURE_CLASSES = (
    "runtime_lost",
    "runtime_error",
    "platform_failed",
    "media_failed",
    "cleanup_failed",
    "safety_failed",
    "control_lost",
)


@dataclass(frozen=True)
class TerminalSettings:
    enabled: bool = False
    callback_url: str = ""
    secret: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TerminalSettings":
        env = os.environ if env is None else env
        return cls(
            enabled=env.get(ENV_ENABLED, "0").strip().lower() in _TRUE,
            callback_url=env.get(ENV_URL, "").strip(),
            secret=env.get(ENV_SECRET, "").strip(),
        )

    @property
    def configured(self) -> bool:
        """True only when the producer may run: flag, secret and a safe callback URL.

        The URL is parsed, not prefix-matched: https anywhere, or http only to an
        exact loopback host, and never with embedded credentials.
        """
        if not (self.enabled and self.secret):
            return False
        try:
            url = urlsplit(self.callback_url)
            host = url.hostname
            url.port  # noqa: B018 - raises ValueError for a malformed port
        except ValueError:
            return False
        if not host or url.username is not None or url.password is not None:
            return False
        return url.scheme == "https" or (url.scheme == "http" and host in _LOOPBACK)


class TerminalPersistError(Exception):
    """The durable terminal record could not be stored; hot state must be kept."""


class TerminalCleanupRetry(Exception):
    """Teardown failed; hot state is kept and the stop reports ending/retry."""


class PersistResult(NamedTuple):
    record: TerminalRecord
    created: bool
    # "" when the stored record was returned unchanged, else the audit kind of the
    # different terminal that was kept as evidence (late_success / late_failure /
    # conflicting_terminal).
    conflict: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _applied_terminal_command(meta: Mapping[str, Any]) -> dict[str, Any] | None:
    """The applied End / Emergency End outcome, if any. Emergency wins over End."""
    outcomes = meta.get("execution_command_outcomes") or {}
    applied = [
        o
        for o in outcomes.values()
        if isinstance(o, dict)
        and o.get("status") == "applied"
        and o.get("command") in _END_REASON_BY_COMMAND
    ]
    applied.sort(key=lambda o: o["command"] != "emergency_end")
    return applied[0] if applied else None


def build_terminal_record(
    meta: Mapping[str, Any] | None,
    *,
    now: datetime,
    cleanup: Cleanup,
    default_failure: str = "control_lost",
) -> TerminalRecord | None:
    """Derive the terminal record from hot state, or None for a legacy session.

    Only what the state proves is claimed. A terminal phase is used as recorded;
    an applied End / Emergency End gives the matching ENDED reason; anything
    else cannot be proven to be a clean end and is recorded as
    ``failed(<default_failure>)`` rather than claiming ``ended``. Phase
    timestamps the hot state does not keep stay null, never guessed. The cleanup
    is the caller's observed teardown result, never assumed.
    """
    raw = (meta or {}).get("execution_contract")
    if not raw:
        return None
    state = ExecutionState.model_validate(raw)
    identity = ExecutionIdentity(**state.model_dump(include=set(ExecutionIdentity.model_fields)))
    command = _applied_terminal_command(meta or {})
    command_ref = None
    stop_requested_at = None
    if command is not None:
        command_ref = CommandRef(command_id=command["command_id"], actor_id=command["actor_id"])
        stop_requested_at = datetime.fromisoformat(
            str(command["requested_at"]).replace("Z", "+00:00")
        )

    reason: str | None = None
    if state.phase == "ended" and state.terminal_reason in _END_REASONS:
        reason = state.terminal_reason
    elif state.phase != "failed" and command is not None:
        # FLAG-018-1: the system-originated reason bound to the applied command.
        reason = (
            command["end_reason"]
            if command.get("end_reason") == "entitlement_exhausted"
            else _END_REASON_BY_COMMAND[command["command"]]
        )

    common: dict[str, Any] = dict(
        identity=identity,
        terminal_sequence=state.sequence,
        first_ai_broadcast=state.first_ai_broadcast,
        first_playable_evidence_id=state.first_playable_evidence_id,
        stop_requested_at=stop_requested_at,
        terminal_at=now,
        recorded_at=now,
        cleanup=cleanup,
        evidence_refs=EvidenceRefs(last_execution_sequence=state.sequence),
    )
    if reason is not None:
        record = TerminalRecord(
            **common,
            terminal_phase="ended",
            reason_code=reason,
            business_outcome="ENDED",
            source="merchant_command" if command_ref else "runtime",
            command_ref=command_ref,
        )
    else:
        hinted = str((meta or {}).get("execution_failure_class") or "")
        record = TerminalRecord(
            **common,
            terminal_phase="failed",
            reason_code="execution_failed",
            failure_class=hinted
            if hinted in _FAILURE_CLASSES
            else ("runtime_error" if state.phase == "failed" else default_failure),
            business_outcome="FAILED",
            source="runtime",
            command_ref=command_ref,
        )
    record = record.sealed()
    validate_terminal_record(record)
    return record


def build_lost_record(
    identity: ExecutionIdentity, *, now: datetime, cleanup: Cleanup
) -> TerminalRecord:
    """failed(runtime_lost) for a registered execution whose hot state is gone."""
    record = TerminalRecord(
        identity=identity,
        terminal_phase="failed",
        reason_code="execution_failed",
        failure_class="runtime_lost",
        business_outcome="FAILED",
        source="runtime",
        terminal_at=now,
        recorded_at=now,
        cleanup=cleanup,
        evidence_refs=EvidenceRefs(diagnostic_ref="hot_state_missing"),
    ).sealed()
    validate_terminal_record(record)
    return record


def backoff_seconds(
    attempts: int,
    *,
    base: float = 1.0,
    cap: float = 300.0,
    rng: Callable[[], float] = random.random,
) -> float:
    """Capped exponential backoff with jitter (implementation defaults, DR-CONFIG-001)."""
    delay = min(cap, base * (2 ** max(0, attempts - 1)))
    return delay * (0.5 + rng() / 2)


class TerminalOutcomes:
    """Persists the terminal record before the caller deletes hot session state."""

    def __init__(self, pg_store: Any, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._pg = pg_store
        self._clock = clock
        # Every P0 execution started by this process, registered durably or not, until its
        # terminal record is stored. It is what the shutdown sweep and a registry-less stop
        # fall back to when the durable registration failed.
        self._active: dict[str, ExecutionIdentity] = {}
        self.pending_registration: set[str] = set()

    @staticmethod
    def _identity_of(meta: Mapping[str, Any]) -> ExecutionIdentity | None:
        raw = meta.get("execution_contract")
        if not raw:
            return None
        state = ExecutionState.model_validate(raw)
        return ExecutionIdentity(**state.model_dump(include=set(ExecutionIdentity.model_fields)))

    async def register(
        self,
        session_id: str,
        meta: Mapping[str, Any],
        *,
        retry_delays: tuple[float, ...] = (0.05, 0.2),
    ) -> None:
        """Durably remember a P0 execution at start.

        The identity is held in memory first. A failing registration is retried
        (bounded); if it still fails the session stays in ``pending_registration`` and an
        audited deferral is attempted, so the stop and shutdown paths can still name it.
        """
        try:
            identity = self._identity_of(meta)
        except Exception as exc:
            logger.warning(
                "terminal identity unreadable session=%s error_type=%s",
                session_id,
                type(exc).__name__,
            )
            return
        if identity is None:
            return
        self._active[session_id] = identity
        error = ""
        for delay in (*retry_delays, None):
            try:
                await self._pg.register_terminal_execution(identity)
                self.pending_registration.discard(session_id)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = type(exc).__name__
                if delay is not None:
                    await asyncio.sleep(delay)
        self.pending_registration.add(session_id)
        logger.error(
            "terminal execution not registered session=%s error_type=%s", session_id, error
        )
        try:
            await self._pg.defer_terminal(session_id, "registration_failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # still tracked in memory for stop and shutdown
            logger.error("registration deferral not stored session=%s", session_id)

    def _forget(self, session_id: str) -> None:
        self._active.pop(session_id, None)
        self.pending_registration.discard(session_id)

    async def settle_cleanup(
        self, session_store: Any, session_id: str, error: str | None
    ) -> Cleanup:
        """Turn the observed teardown result into the cleanup to record.

        A teardown error keeps hot state and raises ``TerminalCleanupRetry`` until
        ``CLEANUP_MAX_ATTEMPTS``; only then is ``failed`` recorded (never a claimed success).
        """
        meta = await session_store.get(session_id)
        attempts = int((meta or {}).get(ATTEMPTS_KEY, 0)) + 1
        if error is None:
            return Cleanup(status="succeeded", attempts=attempts)
        if meta is not None and attempts < CLEANUP_MAX_ATTEMPTS:
            try:
                await session_store.set(
                    session_id, {**meta, ATTEMPTS_KEY: attempts, PENDING_FLAG: True}
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("terminal cleanup marker not stored session=%s", session_id)
            raise TerminalCleanupRetry(error)
        return Cleanup(status="failed", attempts=attempts, last_error_class=error[:64])

    async def persist_before_delete(
        self, session_store: Any, session_id: str, cleanup: Cleanup
    ) -> TerminalRecord | None:
        """Durably store the terminal record + outbox row. None for a legacy session.

        With no hot state the registered identity yields ``failed(runtime_lost)``;
        an unregistered execution gets an explicit audited deferral row. Raises
        ``TerminalPersistError`` when the record cannot be stored; the caller must
        then keep hot state and report ``ending`` with retry. A duplicate stop
        returns the already stored record unchanged.
        """
        meta = await session_store.get(session_id)
        try:
            if meta is None:
                identity = await self._pg.get_terminal_execution(session_id) or self._active.get(
                    session_id
                )
                if identity is None:
                    await self._pg.defer_terminal(session_id, "hot_state_missing_unregistered")
                    logger.error(
                        "terminal deferred: no hot state and no registration session=%s", session_id
                    )
                    return None
                record = build_lost_record(identity, now=self._clock(), cleanup=cleanup)
            else:
                record = build_terminal_record(meta, now=self._clock(), cleanup=cleanup)
                if record is None:
                    return None
            result = await self._pg.persist_terminal(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_pending(session_store, session_id, meta)
            logger.warning(
                "terminal persist failed session=%s error_type=%s", session_id, type(exc).__name__
            )
            raise TerminalPersistError(type(exc).__name__) from exc
        if result.conflict:
            logger.error(
                "terminal conflict kept as evidence session=%s audit=%s",
                session_id,
                result.conflict,
            )
        self._forget(session_id)
        return result.record

    async def persist_active_on_shutdown(
        self,
        session_store: Any,
        extra_session_ids: tuple[str, ...] = (),
        *,
        deferred_session_ids: frozenset[str] = frozenset(),
    ) -> int:
        """Before components stop: give every unterminated execution a durable record.

        Covers the durable registry, every execution this process started (even if its
        registration failed) and every extra session the Runtime still holds (for example
        orchestrators or publishers) whose hot state is a P0 execution. Teardown has not
        happened yet, so cleanup is recorded as pending. A record that cannot be stored is
        left as an explicit audited deferral for the API supervisor (P0-FB-020); it is
        never silently dropped.
        """
        known: dict[str, ExecutionIdentity] = {}
        try:
            for identity in await self._pg.list_unterminated_executions():
                known[identity.runtime_session_id] = identity
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("shutdown registry unreadable error_type=%s", type(exc).__name__)
        known.update(self._active)
        for session_id in extra_session_ids:
            if session_id in known:
                continue
            try:
                identity = self._identity_of((await session_store.get(session_id)) or {})
            except Exception:
                identity = None
            if identity is not None:
                known[session_id] = identity
        stored = 0
        for session_id, identity in known.items():
            if session_id in deferred_session_ids:
                logger.error(
                    "shutdown terminal deferred session=%s error_type=UnsavedRuntimeFailure",
                    session_id,
                )
                continue
            try:
                if await self._pg.terminal_exists(identity):
                    continue
                cleanup = Cleanup(status="pending")
                meta = await session_store.get(identity.runtime_session_id)
                record = None
                if meta:
                    record = build_terminal_record(
                        meta, now=self._clock(), cleanup=cleanup, default_failure="runtime_lost"
                    )
                if record is None:
                    record = build_lost_record(identity, now=self._clock(), cleanup=cleanup)
                await self._pg.persist_terminal(record)
                self._forget(session_id)
                stored += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "shutdown terminal not persisted session=%s error_type=%s",
                    identity.runtime_session_id,
                    type(exc).__name__,
                )
                try:
                    await self._pg.defer_terminal(
                        identity.runtime_session_id, "shutdown_persist_failed"
                    )
                except Exception:  # the log line above is then the only trace
                    logger.error(
                        "shutdown deferral not stored session=%s", identity.runtime_session_id
                    )
        return stored

    async def retry_pending(self, session_store: Any, session_id: str) -> bool:
        """True when an earlier stop already tore the session down but could not finish."""
        meta = await session_store.get(session_id)
        return bool(meta and meta.get(PENDING_FLAG))

    @staticmethod
    async def _mark_pending(
        session_store: Any, session_id: str, meta: Mapping[str, Any] | None
    ) -> None:
        if not meta:
            return
        try:
            await session_store.set(session_id, {**meta, PENDING_FLAG: True})
        except asyncio.CancelledError:
            raise
        except Exception:  # best effort: the stop still reports ending/retry
            logger.warning("terminal pending marker not stored session=%s", session_id)


PostFn = Callable[[str, bytes, Mapping[str, str]], Awaitable[tuple[int, Any]]]


async def _httpx_post(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, Any]:
    import httpx  # already a runtime dependency

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, content=body, headers=dict(headers))
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


class TerminalOutbox:
    """Retryable, restart-safe delivery of stored terminal records to the API.

    No connection or transaction is held while the HTTP call runs: rows are
    claimed with a lease token in one statement, delivered, then finished in
    another that must present the token.
    """

    def __init__(
        self,
        pg_store: Any,
        settings: TerminalSettings,
        *,
        post: PostFn = _httpx_post,
        batch: int = 10,
        poll_seconds: float = 2.0,
        lease_seconds: float = 60.0,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._pg = pg_store
        self._settings = settings
        self._post = post
        self._batch = batch
        self._poll = poll_seconds
        self._lease = lease_seconds
        self._rng = rng

    async def deliver_due(self) -> int:
        rows = await self._pg.claim_terminal_outbox(self._batch, self._lease)
        for row in rows:
            await self._deliver(row)
        return len(rows)

    async def run_loop(self) -> None:
        while True:
            try:
                processed = await self.deliver_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                processed = 0
                logger.warning("terminal outbox pass failed error_type=%s", type(exc).__name__)
            if processed == 0:
                await asyncio.sleep(self._poll)

    async def _deliver(self, row: Mapping[str, Any]) -> None:
        record_id, record_hash = row["terminal_record_id"], row["record_hash"]
        token, kind, attempts = row["lease_token"], row["kind"], int(row["attempts"])
        body: bytes = row["body"].encode()
        headers = {
            "Content-Type": "application/json",
            "X-Livento-Internal-Secret": self._settings.secret,
        }

        async def finish(status: str, *, error: str | None = None, retry_in: float = 0.0) -> None:
            ok = await self._pg.finish_terminal_outbox(
                record_id, record_hash, token, status, error=error, retry_in=retry_in
            )
            if not ok:
                logger.warning("terminal outbox lease lost id=%s; outcome discarded", record_id)

        try:
            status, payload = await self._post(self._settings.callback_url, body, headers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await finish(
                "pending",
                error=f"transport_{type(exc).__name__}",
                retry_in=backoff_seconds(attempts, rng=self._rng),
            )
            return
        if status in (200, 201):
            await finish("delivered")
        elif status == 409:
            code = _error_code(payload)
            if kind == "late_evidence" and code == "already_terminal":
                # The API audited the late evidence: that is the expected answer.
                await finish("delivered")
            else:
                logger.error("terminal record refused by the API id=%s code=%s", record_id, code)
                await finish("rejected", error=f"http_409_{code}")
        elif status in (400, 413, 422):
            logger.error("terminal record invalid for the API id=%s status=%s", record_id, status)
            await finish("rejected", error=f"http_{status}")
        else:  # 401/403/404/429/5xx: possibly clock, secret rotation or an outage; never dropped
            await finish(
                "pending",
                error=f"http_{status}",
                retry_in=backoff_seconds(attempts, rng=self._rng),
            )


def _error_code(payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    code = (data or {}).get("code") if isinstance(data, dict) else None
    ok = isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 40
    return code if ok else "unknown"  # type: ignore[return-value]


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def record_to_body(record: TerminalRecord) -> str:
    """Serialized once at outbox insert; resent byte-for-byte on every attempt."""
    return json.dumps(record.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
