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
from typing import Any, Awaitable, Callable, Mapping

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

_TRUE = ("1", "true", "yes", "on")
_END_REASON_BY_COMMAND = {"end": "normal_end", "emergency_end": "merchant_emergency_end"}
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
        """True only when the producer may run: flag, https-or-loopback URL and secret."""
        return (
            self.enabled
            and bool(self.secret)
            and self.callback_url.startswith(("https://", "http://127.0.0.1", "http://localhost"))
        )


class TerminalPersistError(Exception):
    """The durable terminal record could not be stored; hot state must be kept."""


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
    meta: Mapping[str, Any] | None, *, now: datetime
) -> TerminalRecord | None:
    """Derive the terminal record from hot state, or None for a legacy session.

    Only what the state proves is claimed. A terminal phase is used as recorded;
    an applied End / Emergency End gives the matching ENDED reason; anything
    else cannot be proven to be a clean end and is recorded as the conservative
    ``failed(control_lost)`` rather than claiming ``ended``. Phase timestamps
    the hot state does not keep stay null, never guessed.
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
    if state.phase == "ended" and state.terminal_reason in ("normal_end", "merchant_emergency_end"):
        reason = state.terminal_reason
    elif state.phase != "failed" and command is not None:
        reason = _END_REASON_BY_COMMAND[command["command"]]

    common: dict[str, Any] = dict(
        identity=identity,
        terminal_sequence=state.sequence,
        first_ai_broadcast=state.first_ai_broadcast,
        first_playable_evidence_id=state.first_playable_evidence_id,
        stop_requested_at=stop_requested_at,
        terminal_at=now,
        recorded_at=now,
        # Teardown (backend, LiveKit, detach) completed before the record is written.
        cleanup=Cleanup(status="succeeded", attempts=1),
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
            else ("runtime_error" if state.phase == "failed" else "control_lost"),
            business_outcome="FAILED",
            source="runtime",
            command_ref=command_ref,
        )
    record = record.sealed()
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

    async def persist_before_delete(
        self, session_store: Any, session_id: str
    ) -> TerminalRecord | None:
        """Durably store the terminal record + outbox row. None for a legacy session.

        Raises ``TerminalPersistError`` when the record cannot be stored; the
        caller must then keep hot state and report ``ending`` with retry. A
        duplicate stop returns the already stored record unchanged.
        """
        meta = await session_store.get(session_id)
        try:
            record = build_terminal_record(meta, now=self._clock())
            if record is None:
                return None
            stored, _created = await self._pg.persist_terminal(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_pending(session_store, session_id, meta)
            logger.warning(
                "terminal persist failed session=%s error_type=%s", session_id, type(exc).__name__
            )
            raise TerminalPersistError(type(exc).__name__) from exc
        return stored

    async def retry_pending(self, session_store: Any, session_id: str) -> bool:
        """True when an earlier stop already tore the session down but could not persist."""
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
    claimed with a lease in one statement, delivered, then finished in another.
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
        record_id, attempts = row["terminal_record_id"], int(row["attempts"])
        body: bytes = row["body"].encode()
        headers = {
            "Content-Type": "application/json",
            "X-Livento-Internal-Secret": self._settings.secret,
        }
        try:
            status, payload = await self._post(self._settings.callback_url, body, headers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._retry(record_id, attempts, f"transport_{type(exc).__name__}")
            return
        if status in (200, 201):
            await self._pg.finish_terminal_outbox(record_id, "delivered")
        elif status == 409:
            code = _error_code(payload)
            logger.error("terminal record refused by the API id=%s code=%s", record_id, code)
            await self._pg.finish_terminal_outbox(record_id, "rejected", error=f"http_409_{code}")
        elif status in (400, 413, 422):
            logger.error("terminal record invalid for the API id=%s status=%s", record_id, status)
            await self._pg.finish_terminal_outbox(record_id, "rejected", error=f"http_{status}")
        else:  # 401/403/404/429/5xx: possibly clock, secret rotation or an outage; never dropped
            await self._retry(record_id, attempts, f"http_{status}")

    async def _retry(self, record_id: str, attempts: int, error: str) -> None:
        await self._pg.finish_terminal_outbox(
            record_id,
            "pending",
            error=error,
            retry_in=backoff_seconds(attempts, rng=self._rng),
        )


def _error_code(payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    code = (data or {}).get("code") if isinstance(data, dict) else None
    return (
        code
        if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 40
        else "unknown"
    )


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def record_to_body(record: TerminalRecord) -> str:
    """Serialized once at outbox insert; resent byte-for-byte on every attempt."""
    return json.dumps(record.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
