"""Postgres outbox for usage evidence (C-USAGE-EVIDENCE-001). Storage only.

Every statement is bounded by the pool's command timeout. No connection or
transaction is held while the HTTP delivery runs: rows are leased with a token
in one statement and finished with another that must present the token.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from backend.application.execution_contract import ExecutionIdentity

from . import envelope
from .envelope import Draft

logger = logging.getLogger(__name__)

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
    token: str | None = None  # this staging attempt (None: an already-final row)
    version: int = 0


class StageBlocked(Exception):
    """A committed-but-unresolved attempt owns this semantic row: fail closed, retry later."""


class UsageOutbox:
    """Thin SQL layer over a connected ``PostgresRuntimeStore``."""

    def __init__(self, pg_store: Any, *, producer_id: str = "runtime") -> None:
        self._pg = pg_store
        self._producer = producer_id

    # -- lifecycle rows (control plane) ------------------------------------

    async def stage(
        self,
        identity: ExecutionIdentity,
        drafts: Sequence[Draft],
        *,
        version: int = 1,
        committed_tokens: frozenset[str] | set[str] = frozenset(),
    ) -> list[Staged]:
        """Stage one ATTEMPT per draft, each with its own fresh ``stage_token``.

        A staged row is never reused for a different attempt: re-staging a still-staged
        semantic fact replaces its payload AND token together, so a proof naming the old
        token can never authorize the new bytes (or vice versa). If the old token is
        already in the session's committed proof the old payload is a committed fact and
        must not be replaced: ``StageBlocked`` (the caller answers 503, the sweeper
        resolves it). Rows that are already final (ready, delivered, ...) are returned
        unchanged with ``token=None``; there is nothing to commit for them.

        ``usage_sequence`` is assigned only at ``mark_ready``.
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
                            "SELECT event_id, status, usage_sequence, stage_token "
                            "FROM usage_evidence_outbox "
                            "WHERE (tenant_id, business_session_id, runtime_session_id, generation) "
                            "= ($1, $2, $3, $4) AND kind = $5 AND interval_id = $6 FOR UPDATE",
                            *key,
                            draft.kind,
                            interval,
                        )
                        if existing is not None and existing["status"] not in (
                            "staged",
                            "discarded",
                        ):
                            out.append(
                                Staged(
                                    existing["event_id"],
                                    existing["status"],
                                    existing["usage_sequence"],
                                    False,
                                )
                            )
                            continue
                        if (
                            existing is not None
                            and existing["status"] == "staged"
                            and existing["stage_token"] in committed_tokens
                        ):
                            raise StageBlocked(existing["event_id"])
                        token = uuid.uuid4().hex
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
                                     execution_sequence, applied_sequence, occurred_at,
                                     body, body_sha256, stage_token, staged_version)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
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
                                token,
                                int(version),
                            )
                        else:
                            await conn.execute(
                                """
                                UPDATE usage_evidence_outbox
                                   SET status = 'staged', usage_sequence = NULL,
                                       execution_sequence = $2, applied_sequence = $3,
                                       occurred_at = $4, body = $5, body_sha256 = $6,
                                       stage_token = $7, staged_version = $8,
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
                                token,
                                int(version),
                            )
                        out.append(Staged(eid, "staged", None, True, token, int(version)))
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

    async def mark_ready(self, items: Sequence[tuple[str, str]]) -> list[str]:
        """``staged`` -> ``ready`` for the given ``(event_id, stage_token)`` attempts.

        Numbering is strictly in COMMIT order per identity: a row is numbered only when no
        other still-staged row of the same identity has a lower ``staged_version`` (such a
        row may yet turn out committed; this one waits, still ``staged``, for the sweeper).
        Within one call rows are numbered in ``staged_version`` order, under the
        per-identity counter lock; the final body bytes are fixed here and never change.
        A row discarded by a lost race is REVIVED here (the caller holds proof of commit);
        that is logged as an error. Returns the event_ids that became ready.
        """
        pairs = list(items)
        if not pairs:
            return []
        ids = [p[0] for p in pairs]
        tokens = {p[0]: p[1] for p in pairs}

        async def run() -> list[str]:
            async with self._pg._require_pool().acquire() as conn:
                async with conn.transaction():
                    fetched = await conn.fetch(
                        f"SELECT event_id, status, stage_token, staged_version, body, "
                        f"{_ID_COLUMNS} FROM usage_evidence_outbox "
                        "WHERE event_id = ANY($1::text[]) AND status IN ('staged', 'discarded') "
                        "ORDER BY event_id FOR UPDATE",
                        ids,
                    )
                    rows = [r for r in fetched if r["stage_token"] == tokens[r["event_id"]]]
                    if not rows:
                        return []
                    keys = sorted({tuple(r[c] for c in _ID_NAMES) for r in rows})
                    last: dict[tuple[str, ...], int] = {}
                    horizon: dict[tuple[str, ...], int | None] = {}
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
                        horizon[k] = await conn.fetchval(
                            "SELECT min(staged_version) FROM usage_evidence_outbox "
                            "WHERE (tenant_id, business_session_id, runtime_session_id, generation) "
                            "= ($1, $2, $3, $4) AND status = 'staged' "
                            "AND NOT (event_id = ANY($5::text[]))",
                            *k,
                            [r["event_id"] for r in rows],
                        )
                    flipped: list[str] = []
                    for row in sorted(rows, key=lambda r: (r["staged_version"], r["event_id"])):
                        k = tuple(row[c] for c in _ID_NAMES)
                        limit = horizon[k]
                        if limit is not None and row["staged_version"] > limit:
                            continue  # an earlier, unresolved row must be numbered first
                        if row["status"] == "discarded":
                            logger.error(
                                "usage evidence REVIVED a discarded row (proof shows commit) id=%s",
                                row["event_id"],
                            )
                        last[k] += 1
                        body = envelope.finalize_body(bytes(row["body"]), last[k])
                        await conn.execute(
                            "UPDATE usage_evidence_outbox SET status = 'ready', usage_sequence = $2, "
                            "body = $3, body_sha256 = $4, next_attempt_at = NOW(), "
                            "updated_at = NOW() WHERE event_id = $1",
                            row["event_id"],
                            last[k],
                            body,
                            hashlib.sha256(body).hexdigest(),
                        )
                        flipped.append(row["event_id"])
                    for k, value in last.items():
                        await conn.execute(
                            "UPDATE usage_evidence_sequence SET last_usage_sequence = $5 "
                            "WHERE (tenant_id, business_session_id, runtime_session_id, generation) "
                            "= ($1, $2, $3, $4)",
                            *k,
                            value,
                        )
                    return flipped

        return await self._pg._command(run)

    async def release(self, items: Sequence[tuple[str, str]], *, delete: bool) -> int:
        """Release staged ATTEMPTS that never became a fact (delete, or tombstone).

        Matches the attempt's ``stage_token``: a newer attempt on the same semantic row is
        never touched. Staged rows hold no usage number, so this cannot leave a gap.
        """
        pairs = list(items)
        if not pairs:
            return 0

        async def run() -> int:
            async with self._pg._require_pool().acquire() as conn:
                verb = (
                    "DELETE FROM usage_evidence_outbox o USING t"
                    if delete
                    else "UPDATE usage_evidence_outbox o SET status = 'discarded', "
                    "usage_sequence = NULL, updated_at = NOW() FROM t"
                )
                result = await conn.execute(
                    "WITH t AS (SELECT * FROM unnest($1::text[], $2::text[]) AS x(event_id, token)) "
                    + verb
                    + " WHERE o.event_id = t.event_id AND o.stage_token = t.token "
                    "AND o.status = 'staged'",
                    [p[0] for p in pairs],
                    [p[1] for p in pairs],
                )
                return int(result.rsplit(" ", 1)[-1])

        return await self._pg._command(run)

    async def still_staged(self, tokens: Sequence[str]) -> set[str]:
        """Which of these attempts are still unresolved (their proof must be kept)."""
        if not tokens:
            return set()

        async def run() -> list[Any]:
            async with self._pg._require_pool().acquire() as conn:
                return await conn.fetch(
                    "SELECT stage_token FROM usage_evidence_outbox "
                    "WHERE stage_token = ANY($1::text[]) AND status = 'staged'",
                    list(tokens),
                )

        return {r["stage_token"] for r in await self._pg._command(run)}

    async def get_staged(self, event_id: str) -> dict[str, Any] | None:
        """Re-read one row (under the session lock) before the sweeper decides about it."""

        async def run() -> Any:
            async with self._pg._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    "SELECT event_id, status, stage_token, staged_version "
                    "FROM usage_evidence_outbox WHERE event_id = $1",
                    event_id,
                )

        row = await self._pg._command(run)
        return None if row is None else dict(row)

    async def stale_staged(
        self, older_than_seconds: float, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Staged rows due for resolution, least-recently-tried first (fairness).

        Parked rows carry a future ``next_attempt_at`` so they cannot starve the rest.
        """

        async def run() -> list[Any]:
            async with self._pg._require_pool().acquire() as conn:
                return await conn.fetch(
                    f"SELECT event_id, {_ID_COLUMNS}, applied_sequence, attempts, stage_token, "
                    "staged_version, "
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
                    "AS age, count(*) FILTER (WHERE status = 'staged' AND last_error IS NOT NULL) AS parked "
                    "FROM usage_evidence_outbox WHERE status IN ('staged', 'ready')"
                )

        row = await self._pg._command(run)
        return {
            "count": int(row["n"]),
            "oldest_age_seconds": float(row["age"]),
            "unresolved_parked": int(row["parked"]),  # operator-visible: needs attention
        }
