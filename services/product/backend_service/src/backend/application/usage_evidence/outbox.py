"""Postgres outbox for usage evidence (C-USAGE-EVIDENCE-001). Storage only.

Every statement is bounded by the pool's command timeout. No connection or
transaction is held while the HTTP delivery runs: rows are leased with a token
in one statement and finished with another that must present the token.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from backend.application.execution_contract import ExecutionIdentity

from . import envelope
from .envelope import Draft

_ID_COLUMNS = "tenant_id, business_session_id, runtime_session_id, generation"
_ID_NAMES = ("tenant_id", "business_session_id", "runtime_session_id", "generation")


def _key(identity: ExecutionIdentity) -> tuple[str, str, str, str]:
    return (
        identity.tenant_id,
        identity.business_session_id,
        identity.runtime_session_id,
        identity.generation,
    )


@dataclass(frozen=True)
class Staged:
    event_id: str
    status: str
    usage_sequence: int | None
    created: bool


class UsageOutbox:
    """Thin SQL layer over a connected ``PostgresRuntimeStore``."""

    def __init__(self, pg_store: Any, *, producer_id: str = "runtime") -> None:
        self._pg = pg_store
        self._producer = producer_id

    # -- lifecycle rows (control plane) ------------------------------------

    async def stage(self, identity: ExecutionIdentity, drafts: Sequence[Draft]) -> list[Staged]:
        """Insert rows as ``staged`` (no usage number yet), or return the existing ones.

        ``usage_sequence`` is assigned only when a row becomes ``ready`` (see
        ``mark_ready``), so a staged row that is later discarded can never leave a gap.
        The staged body carries a placeholder number (0). A re-derivation returns the
        stored row (same event_id; event_id does not depend on usage_sequence). A
        ``discarded`` row is revived as ``staged``.
        """
        envelope.validate_identity(identity)
        key = _key(identity)

        async def run() -> list[Staged]:
            async with self._pg._require_pool().acquire() as conn:
                async with conn.transaction():
                    out: list[Staged] = []
                    for draft in drafts:
                        opening = draft.opening_ref
                        if opening is None:
                            opening = await self._open_unusable(conn, key)
                            if opening is None:
                                continue  # no stored start to close: nothing truthful to say
                        interval = envelope.interval_id(identity, draft.interval_kind, opening)
                        existing = await conn.fetchrow(
                            "SELECT event_id, status, usage_sequence FROM usage_evidence_outbox "
                            "WHERE (tenant_id, business_session_id, runtime_session_id, generation) "
                            "= ($1, $2, $3, $4) AND kind = $5 AND interval_id = $6 FOR UPDATE",
                            *key,
                            draft.kind,
                            interval,
                        )
                        if existing is not None and existing["status"] != "discarded":
                            out.append(
                                Staged(
                                    existing["event_id"],
                                    existing["status"],
                                    existing["usage_sequence"],
                                    False,
                                )
                            )
                            continue
                        eid, body = envelope.build_body(
                            identity,
                            draft,
                            interval=interval,
                            usage_sequence=0,
                            producer_id=self._producer,
                        )
                        sha = hashlib.sha256(body).hexdigest()
                        if existing is None:
                            await conn.execute(
                                f"""
                                INSERT INTO usage_evidence_outbox
                                    (event_id, event_type, {_ID_COLUMNS}, kind, interval_id,
                                     execution_sequence, applied_sequence,
                                     occurred_at, body, body_sha256)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                                """,
                                eid,
                                envelope.EVENT_TYPE,
                                *key,
                                draft.kind,
                                interval,
                                draft.execution_sequence,
                                draft.applied_sequence,
                                draft.occurred_at,
                                body,
                                sha,
                            )
                        else:
                            await conn.execute(
                                """
                                UPDATE usage_evidence_outbox
                                   SET status = 'staged', usage_sequence = NULL,
                                       execution_sequence = $2, applied_sequence = $3,
                                       occurred_at = $4, body = $5, body_sha256 = $6,
                                       attempts = 0, last_status = NULL, last_error = NULL,
                                       next_attempt_at = NOW(),
                                       created_at = NOW(), updated_at = NOW()
                                 WHERE event_id = $1
                                """,
                                eid,
                                draft.execution_sequence,
                                draft.applied_sequence,
                                draft.occurred_at,
                                body,
                                sha,
                            )
                        out.append(Staged(eid, "staged", None, True))
                    return out

        return await self._pg._command(run)

    @staticmethod
    async def _open_unusable(conn: Any, key: tuple[str, ...]) -> str | None:
        seq = await conn.fetchval(
            "SELECT execution_sequence FROM usage_evidence_outbox "
            "WHERE (tenant_id, business_session_id, runtime_session_id, generation) "
            "= ($1, $2, $3, $4) AND kind = 'unusable_started' AND status <> 'discarded' "
            "ORDER BY execution_sequence DESC LIMIT 1",
            *key,
        )
        return None if seq is None else str(seq)

    async def mark_ready(self, event_ids: Sequence[str]) -> int:
        """``staged`` -> ``ready``, numbering each row now, in the order given.

        The usage number and the final body bytes are fixed here, atomically, under
        the per-identity counter lock; the bytes never change afterwards. Rows of one
        identity get consecutive numbers, so delivery stays gap-free.
        """
        ids = list(event_ids)

        async def run() -> int:
            async with self._pg._require_pool().acquire() as conn:
                async with conn.transaction():
                    rows = {
                        r["event_id"]: r
                        for r in await conn.fetch(
                            f"SELECT event_id, {_ID_COLUMNS}, body FROM usage_evidence_outbox "
                            "WHERE event_id = ANY($1::text[]) AND status = 'staged' "
                            "ORDER BY event_id FOR UPDATE",
                            ids,
                        )
                    }
                    if not rows:
                        return 0
                    keys = sorted({tuple(r[c] for c in _ID_NAMES) for r in rows.values()})
                    last: dict[tuple[str, ...], int] = {}
                    for k in keys:  # sorted: a stable lock order
                        await conn.execute(
                            f"INSERT INTO usage_evidence_sequence ({_ID_COLUMNS}) "
                            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
                            *k,
                        )
                        last[k] = await conn.fetchval(
                            "SELECT last_usage_sequence FROM usage_evidence_sequence "
                            "WHERE (tenant_id, business_session_id, runtime_session_id, generation) "
                            "= ($1, $2, $3, $4) FOR UPDATE",
                            *k,
                        )
                    n = 0
                    for eid in ids:
                        row = rows.pop(eid, None)
                        if row is None:
                            continue
                        k = tuple(row[c] for c in _ID_NAMES)
                        last[k] += 1
                        body = envelope.finalize_body(bytes(row["body"]), last[k])
                        await conn.execute(
                            "UPDATE usage_evidence_outbox SET status = 'ready', usage_sequence = $2, "
                            "body = $3, body_sha256 = $4, next_attempt_at = NOW(), "
                            "updated_at = NOW() WHERE event_id = $1",
                            eid,
                            last[k],
                            body,
                            hashlib.sha256(body).hexdigest(),
                        )
                        n += 1
                    for k, value in last.items():
                        await conn.execute(
                            "UPDATE usage_evidence_sequence SET last_usage_sequence = $5 "
                            "WHERE (tenant_id, business_session_id, runtime_session_id, generation) "
                            "= ($1, $2, $3, $4)",
                            *k,
                            value,
                        )
                    return n

        return await self._pg._command(run)

    async def release(self, event_ids: Sequence[str], *, delete: bool) -> int:
        """Release staged rows that never became a fact (delete, or tombstone as discarded).

        Staged rows hold no usage number, so releasing one cannot leave a gap.
        """

        async def run() -> int:
            async with self._pg._require_pool().acquire() as conn:
                if delete:
                    sql = (
                        "DELETE FROM usage_evidence_outbox "
                        "WHERE event_id = ANY($1::text[]) AND status = 'staged'"
                    )
                else:
                    sql = (
                        "UPDATE usage_evidence_outbox SET status = 'discarded', "
                        "usage_sequence = NULL, updated_at = NOW() "
                        "WHERE event_id = ANY($1::text[]) AND status = 'staged'"
                    )
                result = await conn.execute(sql, list(event_ids))
                return int(result.rsplit(" ", 1)[-1])

        return await self._pg._command(run)

    async def stale_staged(
        self, older_than_seconds: float, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Staged rows due for resolution, least-recently-tried first (fairness).

        Parked rows carry a future ``next_attempt_at`` so they cannot starve the rest.
        """

        async def run() -> list[Any]:
            async with self._pg._require_pool().acquire() as conn:
                return await conn.fetch(
                    f"SELECT event_id, {_ID_COLUMNS}, applied_sequence, attempts, "
                    "EXTRACT(EPOCH FROM (NOW() - created_at))::float8 AS age_seconds "
                    "FROM usage_evidence_outbox "
                    "WHERE status = 'staged' AND created_at <= NOW() - make_interval(secs => $1) "
                    "AND next_attempt_at <= NOW() "
                    "ORDER BY next_attempt_at, created_at LIMIT $2",
                    float(older_than_seconds),
                    int(limit),
                )

        return [dict(r) for r in await self._pg._command(run)]

    async def park(self, event_ids: Sequence[str], *, reason: str, retry_in: float) -> int:
        """Leave staged rows unresolved: back off, count the attempt, record why."""

        async def run() -> int:
            async with self._pg._require_pool().acquire() as conn:
                result = await conn.execute(
                    "UPDATE usage_evidence_outbox SET attempts = attempts + 1, last_error = $2, "
                    "next_attempt_at = NOW() + make_interval(secs => LEAST($3::float8 * "
                    "power(2, LEAST(attempts, 16)), 3600)), updated_at = NOW() "
                    "WHERE event_id = ANY($1::text[]) AND status = 'staged'",
                    list(event_ids),
                    reason,
                    float(retry_in),
                )
                return int(result.rsplit(" ", 1)[-1])

        return await self._pg._command(run)

    async def purge(self, older_than_days: float, limit: int) -> int:
        """Retention: delete finished rows (delivered, permanently failed, discarded)."""

        async def run() -> int:
            async with self._pg._require_pool().acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM usage_evidence_outbox WHERE event_id IN ("
                    "SELECT event_id FROM usage_evidence_outbox "
                    "WHERE status IN ('delivered', 'conflict', 'rejected', 'discarded') "
                    "AND updated_at < NOW() - make_interval(secs => $1) "
                    "ORDER BY updated_at LIMIT $2)",
                    float(older_than_days) * 86400.0,
                    int(limit),
                )
                return int(result.rsplit(" ", 1)[-1])

        return await self._pg._command(run)

    # -- vendor COGS (drained from the in-memory buffer by the sender task) --

    async def enqueue_cogs(
        self,
        identity: ExecutionIdentity,
        *,
        event_id: str,
        sample_id: str,
        occurred_at: datetime,
        body: bytes,
    ) -> bool:
        envelope.validate_identity(identity)
        sha = hashlib.sha256(body).hexdigest()

        async def run() -> bool:
            async with self._pg._require_pool().acquire() as conn:
                result = await conn.execute(
                    f"""
                    INSERT INTO usage_evidence_outbox
                        (event_id, event_type, {_ID_COLUMNS}, kind, interval_id,
                         occurred_at, body, body_sha256, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'cogs', $7, $8, $9, $10, 'ready')
                    ON CONFLICT DO NOTHING
                    """,
                    event_id,
                    envelope.COGS_EVENT_TYPE,
                    *_key(identity),
                    sample_id,
                    occurred_at,
                    body,
                    sha,
                )
                return result.endswith(" 1")

        return await self._pg._command(run)

    # -- delivery ------------------------------------------------------------

    async def claim(self, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        """Lease due rows. Lifecycle rows go out per identity in usage_sequence order:
        a row is eligible only if no lower-numbered sibling is still staged or ready.
        Delivered, conflict, rejected and discarded siblings never block it."""

        async def run() -> list[Any]:
            async with self._pg._require_pool().acquire() as conn:
                return await conn.fetch(
                    """
                    WITH due AS (
                        SELECT o.event_id FROM usage_evidence_outbox o
                        WHERE o.status = 'ready' AND o.next_attempt_at <= NOW()
                          AND (o.usage_sequence IS NULL OR NOT EXISTS (
                                SELECT 1 FROM usage_evidence_outbox p
                                WHERE p.tenant_id = o.tenant_id
                                  AND p.business_session_id = o.business_session_id
                                  AND p.runtime_session_id = o.runtime_session_id
                                  AND p.generation = o.generation
                                  AND p.usage_sequence < o.usage_sequence
                                  AND p.status IN ('staged', 'ready')))
                        ORDER BY o.created_at LIMIT $1 FOR UPDATE OF o SKIP LOCKED
                    )
                    UPDATE usage_evidence_outbox o
                       SET attempts = o.attempts + 1,
                           lease_token = gen_random_uuid(),
                           next_attempt_at = NOW() + make_interval(secs => $2),
                           updated_at = NOW()
                      FROM due WHERE o.event_id = due.event_id
                 RETURNING o.event_id, o.body, o.attempts, o.usage_sequence,
                           o.lease_token::text AS lease_token
                    """,
                    int(limit),
                    float(lease_seconds),
                )

        return [dict(r) for r in await self._pg._command(run)]

    async def finish(
        self,
        event_id: str,
        lease_token: str,
        status: str,
        *,
        http_status: int | None = None,
        error: str | None = None,
        retry_in: float = 0.0,
    ) -> bool:
        """Finish a leased row. ``status='ready'`` reschedules it with the same bytes.

        Only the current lease holder of a still-ready row may finish it, so a
        delivered or permanently classified row is never resurrected.
        """

        async def run() -> bool:
            async with self._pg._require_pool().acquire() as conn:
                row = await conn.fetchval(
                    """
                    UPDATE usage_evidence_outbox
                       SET status = $3, last_status = $4, last_error = $5, lease_token = NULL,
                           next_attempt_at = NOW() + make_interval(secs => $6),
                           delivered_at = CASE WHEN $3 = 'delivered' THEN NOW() ELSE delivered_at END,
                           updated_at = NOW()
                     WHERE event_id = $1 AND lease_token = $2::uuid AND status = 'ready'
                 RETURNING 1
                    """,
                    event_id,
                    lease_token,
                    status,
                    http_status,
                    error,
                    float(retry_in),
                )
                return row is not None

        return bool(await self._pg._command(run))

    async def backlog(self) -> Mapping[str, float]:
        """Undelivered rows: count and age of the oldest, for health (P0-FB-020)."""

        async def run() -> Any:
            async with self._pg._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    "SELECT count(*) AS n, COALESCE(EXTRACT(EPOCH FROM (NOW() - min(created_at))), 0) "
                    "AS age FROM usage_evidence_outbox WHERE status IN ('staged', 'ready')"
                )

        row = await self._pg._command(run)
        return {"count": int(row["n"]), "oldest_age_seconds": float(row["age"])}
