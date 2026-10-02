"""HMAC signer, background sender and staged-row sweeper (C-USAGE-EVIDENCE-001).

The sender is a background task: nothing here runs on the speech/decision path.
It matches the existing API receiver exactly: lowercase hex
``HMAC-SHA256(secret, X-AI-Timestamp + "." + raw body)``, unix-second timestamp,
``X-AI-Event-Id`` equal to the body ``event_id``, and the stored bytes resent
verbatim on every attempt (only the timestamp and signature change).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import time
from typing import Any, Awaitable, Callable, Mapping

from backend.application.execution_contract import ExecutionState
from backend.application.terminal_outcomes import backoff_seconds

from . import envelope
from .outbox import UsageOutbox
from .settings import CogsBuffer, UsageEvidenceSettings

logger = logging.getLogger(__name__)

# Session-meta key listing the usage event_ids committed by the state save (bounded).
COMMITTED_KEY = "usage_evidence_committed"
COMMITTED_MAX = 256
_IDENTITY_KEYS = ("tenant_id", "business_session_id", "runtime_session_id", "generation")
_SWEEP_LIMIT = 100
_SWEEP_BATCHES = 10

PostFn = Callable[[str, bytes, Mapping[str, str]], Awaitable[int]]


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def signed_headers(secret: str, event_id: str, body: bytes, now: float) -> dict[str, str]:
    timestamp = str(int(now))
    return {
        "Content-Type": "application/json",
        "X-AI-Event-Id": event_id,
        "X-AI-Timestamp": timestamp,
        "X-AI-Signature": sign(secret, timestamp, body),
    }


class HttpxPoster:
    """One reused client; closed with the sender. Redirects are never followed."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._client: Any = None

    async def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        if self._client is None:
            import httpx  # already a runtime dependency

            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        response = await self._client.post(url, content=body, headers=dict(headers))
        return response.status_code

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


class UsageSender:
    def __init__(
        self,
        outbox: UsageOutbox,
        settings: UsageEvidenceSettings,
        *,
        session_store: Any = None,
        cogs: CogsBuffer | None = None,
        post: PostFn | None = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        batch: int = 10,
    ) -> None:
        self._outbox = outbox
        self._settings = settings
        self._store = session_store
        self._cogs = cogs
        self._poster = HttpxPoster(settings.http_timeout) if post is None else None
        self._post: PostFn = post or self._poster  # type: ignore[assignment]
        self._clock = clock
        self._rng = rng
        self._batch = batch
        self._last_sweep = 0.0
        self.permanent_failures = 0

    # -- one delivery pass -------------------------------------------------

    async def deliver_due(self) -> int:
        rows = await self._outbox.claim(self._batch, self._settings.lease_seconds)
        for row in rows:
            await self._deliver(row)
        return len(rows)

    async def _deliver(self, row: Mapping[str, Any]) -> None:
        eid, token, attempts = row["event_id"], row["lease_token"], int(row["attempts"])
        body = bytes(row["body"])
        headers = signed_headers(self._settings.secret, eid, body, self._clock())

        async def finish(status: str, *, http: int | None, error: str | None, retry: float = 0.0):
            ok = await self._outbox.finish(
                eid, token, status, http_status=http, error=error, retry_in=retry
            )
            if not ok:
                logger.warning("usage evidence lease lost id=%s; outcome discarded", eid)

        def retry_delay() -> float:
            return backoff_seconds(
                attempts,
                base=self._settings.backoff_base,
                cap=self._settings.backoff_cap,
                rng=self._rng,
            )

        try:
            status = await self._post(self._settings.url, body, headers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # timeout, refused, DNS: never dropped
            await finish(
                "ready", http=None, error=f"transport_{type(exc).__name__}", retry=retry_delay()
            )
            return
        if status == 202:  # accepted, or duplicate / already_processing: durably claimed
            await finish("delivered", http=status, error=None)
        elif status == 409:  # same event_id, different bytes: a producer bug; never resend
            self.permanent_failures += 1
            logger.error("usage evidence CONFLICT id=%s; marked permanent, not resent", eid)
            await finish("conflict", http=status, error="http_409")
        elif status in (400, 413):
            self.permanent_failures += 1
            logger.error("usage evidence rejected id=%s status=%s; not retried", eid, status)
            await finish("rejected", http=status, error=f"http_{status}")
        else:  # 401 (clock/secret), 503, 5xx, anything unexpected: retry, re-signed
            if status == 401:
                logger.error("usage evidence 401 id=%s: check clock skew or secret rotation", eid)
            await finish("ready", http=status, error=f"http_{status}", retry=retry_delay())

    # -- staged-row sweeper (Redis meta <-> Postgres commit gap) -------------

    async def sweep(self) -> int:
        """Resolve staged rows older than the configured age, with proof of the fact.

        A staged row becomes ``ready`` only if the session meta lists its ``event_id``
        among the facts committed by the state save (``COMMITTED_KEY``): a sequence
        number alone never proves which fact landed. A row whose id is absent while
        the state sequence reached the row's ``applied_sequence`` (a different fact
        won that slot), or has not reached it at all, provably never applied and is
        ``discarded``. A row whose session cannot be read or now belongs to another
        generation is *parked* (backoff, reason recorded) so it cannot starve other
        rows, and is dropped with an audit log after ``unresolved_ttl``.
        Finally, finished rows past the retention window are deleted.
        """
        if self._store is None:
            return 0
        resolved = 0
        for _ in range(_SWEEP_BATCHES):
            rows = await self._outbox.stale_staged(self._settings.sweep_age, _SWEEP_LIMIT)
            if not rows:
                break
            resolved += await self._resolve(rows)
            if len(rows) < _SWEEP_LIMIT:
                break
        try:
            await self._outbox.purge(self._settings.retention_days, self._settings.retention_batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("usage evidence retention failed error_type=%s", type(exc).__name__)
        return resolved

    async def _resolve(self, rows: list[dict[str, Any]]) -> int:
        ready: list[dict[str, Any]] = []
        discard: list[str] = []
        parked: dict[str, list[str]] = {}
        expired: list[str] = []
        for row in rows:
            reason = None
            try:
                meta = await self._store.get(row["runtime_session_id"])
                raw = (meta or {}).get("execution_contract")
                state = ExecutionState.model_validate(raw) if raw else None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("usage sweep unreadable error_type=%s", type(exc).__name__)
                state, meta, reason = None, None, "session_unreadable"
            if state is None and reason is None:
                reason = "session_missing"
            elif state is not None and (
                envelope.identity_of(state).model_dump() != {k: row[k] for k in _IDENTITY_KEYS}
            ):
                reason = "generation_changed"
            if reason is not None:
                if float(row.get("age_seconds") or 0) >= self._settings.unresolved_ttl:
                    expired.append(row["event_id"])
                    logger.error(
                        "usage evidence AUDIT dropped unresolvable staged row id=%s reason=%s",
                        row["event_id"],
                        reason,
                    )
                else:
                    parked.setdefault(reason, []).append(row["event_id"])
                continue
            committed = (meta or {}).get(COMMITTED_KEY) or []
            if row["event_id"] in committed:
                ready.append(row)
            else:
                discard.append(row["event_id"])
        if ready:
            ready.sort(key=lambda r: int(r["applied_sequence"]))  # apply order within a pass
            await self._outbox.mark_ready([r["event_id"] for r in ready])
        if discard or expired:
            await self._outbox.release(discard + expired, delete=False)
        if discard:
            logger.warning("usage evidence discarded %d never-applied staged rows", len(discard))
        for reason, ids in parked.items():
            await self._outbox.park(ids, reason=reason, retry_in=self._settings.sweep_age)
        return len(ready) + len(discard) + len(expired)

    async def drain_cogs(self) -> int:
        if self._cogs is None:
            return 0
        stored = 0
        for sample in self._cogs.drain(self._batch):
            try:
                eid, body = envelope.build_cogs_body(
                    sample.identity,
                    sample_id=sample.sample_id,
                    occurred_at=sample.occurred_at,
                    model_id=sample.model_id,
                    input_tokens=sample.input_tokens,
                    output_tokens=sample.output_tokens,
                    producer_id=self._settings.producer_id,
                )
                await self._outbox.enqueue_cogs(
                    sample.identity,
                    event_id=eid,
                    sample_id=sample.sample_id,
                    occurred_at=sample.occurred_at,
                    body=body,
                )
                stored += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # COGS is internal and may be lost; never retried here
                self._cogs.lost += 1
                logger.warning("usage cogs sample lost error_type=%s", type(exc).__name__)
        return stored

    async def run_loop(self) -> None:
        while True:
            try:
                if self._clock() - self._last_sweep >= self._settings.sweep_interval:
                    self._last_sweep = self._clock()
                    await self.sweep()
                await self.drain_cogs()
                processed = await self.deliver_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                processed = 0
                logger.warning("usage evidence pass failed error_type=%s", type(exc).__name__)
            if processed == 0:
                await asyncio.sleep(self._settings.poll_seconds)

    async def flush(self, timeout: float) -> None:
        """Bounded final delivery pass at shutdown. Unsent rows stay durable."""

        async def passes() -> None:
            await self.drain_cogs()
            while await self.deliver_due():
                pass

        try:
            await asyncio.wait_for(passes(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # timeout included
            logger.warning(
                "usage evidence final flush incomplete error_type=%s", type(exc).__name__
            )

    async def close(self) -> None:
        if self._poster is not None:
            await self._poster.close()
