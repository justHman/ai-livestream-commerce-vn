"""Optional asyncpg runtime store for Postgres (DATABASE_URL).

Copied from ``core/db/postgres_store.py`` (COPY-DON'T-IMPORT, OpenSpec 1.23)
so the canonical backend service is self-contained. The schema SQL now lives
under ``backend/db/sql/runtime_schema.sql``.

Session KV still lives in backend.application.db (memory/redis). This module
persists durable runtime rows: sessions, products snapshot, viewer msgs,
director decisions, llm/tts logs, audit events.

asyncpg is an optional dependency — import only when DATABASE_URL is set.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

_SCHEMA = Path(__file__).resolve().parents[2] / "db" / "sql" / "runtime_schema.sql"
_TERMINAL_SCHEMA = Path(__file__).resolve().parents[2] / "db" / "sql" / "terminal_schema.sql"
_CONNECT_TIMEOUT_SECONDS = 5.0
_COMMAND_TIMEOUT_SECONDS = 5.0


def schema_path() -> Path:
    """Return absolute path to runtime_schema.sql."""
    return _SCHEMA


def schema_sql() -> str:
    """Load schema SQL text (offline-safe)."""
    return _SCHEMA.read_text(encoding="utf-8")


class PostgresRuntimeStore:
    """Thin asyncpg wrapper. No-op construction without DATABASE_URL.

    Methods raise RuntimeError if pool is not connected. Call ``connect()``
    first when DATABASE_URL is present.
    """

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = (database_url or "").strip()
        self._pool = None
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.database_url)

    async def connect(self) -> None:
        if not self.enabled:
            return
        try:
            import asyncpg  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dep
            error = RuntimeError(
                "asyncpg is required when DATABASE_URL is set; pip install asyncpg"
            )
            self._record_error(error)
            raise error from exc

        pool = None
        try:
            pool = await asyncio.wait_for(
                asyncpg.create_pool(
                    self.database_url,
                    min_size=1,
                    max_size=5,
                    command_timeout=_COMMAND_TIMEOUT_SECONDS,
                ),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(exc)
            if pool is not None:
                await pool.close()
            raise
        self._pool = pool
        self.last_error = None

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await asyncio.wait_for(pool.close(), timeout=_COMMAND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            self._record_error(exc)
            raise TimeoutError(str(exc)) from exc
        except Exception as exc:
            self._record_error(exc)
            raise

    async def health(self) -> tuple[bool, Optional[str]]:
        """Run a bounded connectivity check when Postgres is configured."""
        if not self.enabled:
            return True, None
        if self._pool is None:
            return False, self.last_error or "PostgresRuntimeStore not connected"

        async def check() -> None:
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT 1")

        try:
            await asyncio.wait_for(check(), timeout=_COMMAND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(exc)
            return False, self.last_error
        self.last_error = None
        return True, None

    async def apply_schema(self) -> None:
        """Apply runtime_schema.sql (idempotent CREATE IF NOT EXISTS)."""
        sql = schema_sql()

        async def apply() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute(sql)

        await self._command(apply)

    async def apply_terminal_schema(self) -> None:
        """Apply terminal_schema.sql (P0-FB-019). Called only when the feature is enabled."""
        sql = _TERMINAL_SCHEMA.read_text(encoding="utf-8")

        async def apply() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute(sql)

        await self._command(apply)

    async def persist_terminal(self, record: Any) -> Any:
        """Store the terminal record and its outbox row in ONE transaction.

        At most one record exists per (tenant, business session, generation) and
        the first durable record wins. A different terminal for the same
        generation is never dropped: it is appended to ``terminal_conflicts``
        and queued as ``late_evidence`` so the API audits it under the same
        precedence (FAILED is never rewritten to ENDED; a late failure after
        ENDED becomes cleanup evidence there). Returns a ``PersistResult``.
        """
        from backend.application.execution_contract import TerminalRecord, reduce_terminal
        from backend.application.terminal_outcomes import PersistResult, body_sha256, record_to_body

        ident = record.identity
        body = record_to_body(record)

        async def persist() -> Any:
            async with self._require_pool().acquire() as conn:
                async with conn.transaction():
                    inserted = await conn.fetchval(
                        """
                        INSERT INTO terminal_records (
                            terminal_record_id, tenant_id, business_session_id,
                            runtime_session_id, generation, record_hash, record
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                        ON CONFLICT DO NOTHING
                        RETURNING terminal_record_id
                        """,
                        record.terminal_record_id,
                        ident.tenant_id,
                        ident.business_session_id,
                        ident.runtime_session_id,
                        ident.generation,
                        record.record_hash,
                        body,
                    )
                    if inserted is not None:
                        await conn.execute(
                            """
                            INSERT INTO terminal_outbox
                                (terminal_record_id, record_hash, kind, body, body_sha256)
                            VALUES ($1,$2,'primary',$3,$4)
                            """,
                            record.terminal_record_id,
                            record.record_hash,
                            body,
                            body_sha256(body),
                        )
                        return PersistResult(record, True, "")
                    row = await conn.fetchrow(
                        """
                        SELECT record FROM terminal_records
                        WHERE tenant_id = $1 AND business_session_id = $2 AND generation = $3
                        """,
                        ident.tenant_id,
                        ident.business_session_id,
                        ident.generation,
                    )
                    existing = TerminalRecord.model_validate(json.loads(row["record"]))
                    decision = reduce_terminal(existing, record)
                    if decision.action == "replay":
                        return PersistResult(existing, False, "")
                    await conn.execute(
                        """
                        INSERT INTO terminal_conflicts (terminal_record_id, incoming_hash, audit, record)
                        VALUES ($1,$2,$3,$4::jsonb)
                        ON CONFLICT DO NOTHING
                        """,
                        existing.terminal_record_id,
                        record.record_hash,
                        decision.audit,
                        body,
                    )
                    await conn.execute(
                        """
                        INSERT INTO terminal_outbox
                            (terminal_record_id, record_hash, kind, body, body_sha256)
                        VALUES ($1,$2,'late_evidence',$3,$4)
                        ON CONFLICT DO NOTHING
                        """,
                        record.terminal_record_id,
                        record.record_hash,
                        body,
                        body_sha256(body),
                    )
                    return PersistResult(existing, False, decision.audit)

        return await self._command(persist)

    async def claim_terminal_outbox(self, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        """Lease due rows in one statement; no connection is held during delivery.

        Every claim mints a new ``lease_token``; finishing a row requires the
        token, so a sender whose lease expired cannot overwrite a newer outcome.
        """

        async def claim() -> list[Any]:
            async with self._require_pool().acquire() as conn:
                return await conn.fetch(
                    """
                    WITH due AS (
                        SELECT terminal_record_id, record_hash FROM terminal_outbox
                        WHERE status = 'pending' AND next_attempt_at <= NOW()
                        ORDER BY created_at LIMIT $1 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE terminal_outbox o
                       SET attempts = o.attempts + 1,
                           lease_token = gen_random_uuid(),
                           next_attempt_at = NOW() + make_interval(secs => $2),
                           updated_at = NOW()
                      FROM due
                     WHERE o.terminal_record_id = due.terminal_record_id
                       AND o.record_hash = due.record_hash
                 RETURNING o.terminal_record_id, o.record_hash, o.kind, o.body, o.attempts,
                           o.lease_token::text AS lease_token
                    """,
                    limit,
                    float(lease_seconds),
                )

        return [dict(r) for r in await self._command(claim)]

    async def finish_terminal_outbox(
        self,
        terminal_record_id: str,
        record_hash: str,
        lease_token: str,
        status: str,
        *,
        error: Optional[str] = None,
        retry_in: float = 0.0,
    ) -> bool:
        """Finish a claimed row. Only the current lease holder of a still-pending row may.

        Returns False (and changes nothing) for a stale token or a row that is
        already delivered/rejected, so delivered and poison rows are never resurrected.
        """

        async def finish() -> bool:
            async with self._require_pool().acquire() as conn:
                row = await conn.fetchval(
                    """
                    UPDATE terminal_outbox
                       SET status = $4,
                           last_error = $5,
                           lease_token = NULL,
                           next_attempt_at = NOW() + make_interval(secs => $6),
                           delivered_at = CASE WHEN $4 = 'delivered' THEN NOW() ELSE delivered_at END,
                           updated_at = NOW()
                     WHERE terminal_record_id = $1 AND record_hash = $2
                       AND lease_token = $3::uuid AND status = 'pending'
                 RETURNING 1
                    """,
                    terminal_record_id,
                    record_hash,
                    lease_token,
                    status,
                    error,
                    float(retry_in),
                )
                return row is not None

        return bool(await self._command(finish))

    async def update_terminal_cleanup(self, terminal_record_id: str, cleanup: Any) -> bool:
        """Record a cleanup retry: only ``cleanup`` changes, monotonically, and is redelivered.

        The terminal phase, reason and hash are never touched (cleanup retries
        are recorded separately). Returns True when the stored record changed.
        """
        from backend.application.execution_contract import TerminalRecord, reduce_cleanup
        from backend.application.terminal_outcomes import body_sha256, record_to_body

        async def update() -> bool:
            async with self._require_pool().acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT record FROM terminal_records WHERE terminal_record_id = $1 FOR UPDATE",
                        terminal_record_id,
                    )
                    if row is None:
                        raise KeyError(terminal_record_id)
                    stored = TerminalRecord.model_validate(json.loads(row["record"]))
                    merged, changed = reduce_cleanup(stored.cleanup, cleanup)
                    if not changed:
                        return False
                    body = record_to_body(stored.model_copy(update={"cleanup": merged}))
                    await conn.execute(
                        "UPDATE terminal_records SET record = $2::jsonb, updated_at = NOW() "
                        "WHERE terminal_record_id = $1",
                        terminal_record_id,
                        body,
                    )
                    await conn.execute(
                        """
                        UPDATE terminal_outbox
                           SET body = $2, body_sha256 = $3, status = 'pending', attempts = 0,
                               lease_token = NULL, next_attempt_at = NOW(), last_error = NULL,
                               updated_at = NOW()
                         WHERE terminal_record_id = $1 AND kind = 'primary'
                        """,
                        terminal_record_id,
                        body,
                        body_sha256(body),
                    )
                    return True

        return bool(await self._command(update))

    async def register_terminal_execution(self, identity: Any) -> None:
        """Remember a P0 execution durably at start, so a stop or shutdown that finds
        no hot state can still name it (failed/runtime_lost) instead of staying silent."""

        async def register() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO terminal_executions
                        (runtime_session_id, tenant_id, business_session_id, generation)
                    VALUES ($1,$2,$3,$4)
                    ON CONFLICT DO NOTHING
                    """,
                    identity.runtime_session_id,
                    identity.tenant_id,
                    identity.business_session_id,
                    identity.generation,
                )

        await self._command(register)

    async def get_terminal_execution(self, runtime_session_id: str) -> Any:
        from backend.application.execution_contract import ExecutionIdentity

        async def get() -> Any:
            async with self._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    """
                    SELECT tenant_id, business_session_id, runtime_session_id, generation
                      FROM terminal_executions WHERE runtime_session_id = $1
                    """,
                    runtime_session_id,
                )

        row = await self._command(get)
        return None if row is None else ExecutionIdentity(**dict(row))

    async def list_unterminated_executions(self) -> list[Any]:
        """Registered executions with no terminal record yet (active at shutdown)."""
        from backend.application.execution_contract import ExecutionIdentity

        async def listing() -> Any:
            async with self._require_pool().acquire() as conn:
                return await conn.fetch(
                    """
                    SELECT e.tenant_id, e.business_session_id, e.runtime_session_id, e.generation
                      FROM terminal_executions e
                     WHERE NOT EXISTS (
                        SELECT 1 FROM terminal_records r
                         WHERE r.tenant_id = e.tenant_id
                           AND r.business_session_id = e.business_session_id
                           AND r.generation = e.generation)
                    """
                )

        return [ExecutionIdentity(**dict(r)) for r in await self._command(listing)]

    async def defer_terminal(self, runtime_session_id: str, reason: str) -> None:
        """Explicit, audited deferral when a terminal cannot be named (never silent)."""

        async def defer() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO terminal_deferrals (runtime_session_id, reason)
                    VALUES ($1,$2) ON CONFLICT DO NOTHING
                    """,
                    runtime_session_id,
                    reason[:120],
                )

        await self._command(defer)

    async def terminal_outbox_backlog(self) -> tuple[int, float]:
        """(pending rows, age in seconds of the oldest) for Runtime health / P0-FB-020."""

        async def backlog() -> Any:
            async with self._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    """
                    SELECT count(*) AS n,
                           COALESCE(EXTRACT(EPOCH FROM NOW() - MIN(created_at)), 0) AS age
                      FROM terminal_outbox WHERE status = 'pending'
                    """
                )

        row = await self._command(backlog)
        return int(row["n"]), float(row["age"])

    async def _command(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await asyncio.wait_for(operation(), timeout=_COMMAND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(exc)
            raise

    def _record_error(self, exc: Exception) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"

    def _require_pool(self):
        if self._pool is None:
            raise RuntimeError("PostgresRuntimeStore not connected")
        return self._pool

    async def upsert_session(
        self,
        session_id: str,
        *,
        status: str = "created",
        mode: str = "mock",
        render_backend: Optional[str] = None,
        avatar_id: Optional[str] = None,
        room_name: Optional[str] = None,
        owner_instance: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        meta = json.dumps(metadata or {})

        async def upsert() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, status, mode, render_backend, avatar_id,
                        room_name, owner_instance, metadata, updated_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb, NOW())
                    ON CONFLICT (session_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        mode = EXCLUDED.mode,
                        render_backend = EXCLUDED.render_backend,
                        avatar_id = EXCLUDED.avatar_id,
                        room_name = EXCLUDED.room_name,
                        owner_instance = EXCLUDED.owner_instance,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    """,
                    session_id,
                    status,
                    mode,
                    render_backend,
                    avatar_id,
                    room_name,
                    owner_instance,
                    meta,
                )

        await self._command(upsert)

    async def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        async def get() -> Any:
            async with self._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    "SELECT * FROM sessions WHERE session_id = $1", session_id
                )

        row = await self._command(get)
        return dict(row) if row is not None else None

    async def insert_product_snapshot(
        self,
        session_id: str,
        products: list[dict[str, Any]],
    ) -> None:
        """Persist the frozen product snapshot for a session (idempotent upsert).

        Called once at /lite/attach. Rows are frozen for the livestream lifetime
        (replay correctness + price integrity) — never mutated mid-stream.
        """

        async def insert() -> None:
            async with self._require_pool().acquire() as conn:
                for idx, p in enumerate(products):
                    pid = str(p.get("id") or p.get("product_id") or "")
                    name = p.get("name")
                    price = p.get("price")
                    payload = {k: v for k, v in p.items() if k not in ("id", "name", "price")}
                    await conn.execute(
                        """
                        INSERT INTO session_products (
                            session_id, product_id, name, price, payload, sort_order
                        ) VALUES ($1,$2,$3,$4,$5::jsonb,$6)
                        ON CONFLICT (session_id, product_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            price = EXCLUDED.price,
                            payload = EXCLUDED.payload,
                            sort_order = EXCLUDED.sort_order
                        """,
                        session_id,
                        pid,
                        name,
                        price,
                        json.dumps(payload),
                        idx,
                    )

        await self._command(insert)

    async def insert_viewer_msg(
        self,
        session_id: str,
        text: str,
        *,
        author: Optional[str] = None,
        comment_id: Optional[str] = None,
        source: str = "platform",
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        async def insert() -> Any:
            async with self._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    """
                    INSERT INTO viewer_msgs (
                        session_id, comment_id, author, text, source, payload
                    ) VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                    RETURNING id
                    """,
                    session_id,
                    comment_id,
                    author,
                    text,
                    source,
                    json.dumps(payload or {}),
                )

        row = await self._command(insert)
        return int(row["id"])

    async def insert_director_decision(
        self,
        session_id: str,
        action: str,
        *,
        product_id: Optional[str] = None,
        score: Optional[float] = None,
        phase: Optional[str] = None,
        product_idx: Optional[int] = None,
        talking_point_idx: Optional[int] = None,
        utterance: Optional[str] = None,
        reason: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        async def insert() -> Any:
            async with self._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    """
                    INSERT INTO director_decisions (
                        session_id, action, product_id, score, phase,
                        product_idx, talking_point_idx, utterance, reason, payload
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
                    RETURNING id
                    """,
                    session_id,
                    action,
                    product_id,
                    score,
                    phase,
                    product_idx,
                    talking_point_idx,
                    utterance,
                    reason,
                    json.dumps(payload or {}),
                )

        row = await self._command(insert)
        return int(row["id"])

    async def insert_audit_event(
        self,
        event_type: str,
        *,
        session_id: Optional[str] = None,
        actor: Optional[str] = None,
        resource: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> int:
        async def insert() -> Any:
            async with self._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    """
                    INSERT INTO audit_events (
                        session_id, actor, event_type, resource, detail
                    ) VALUES ($1,$2,$3,$4,$5::jsonb)
                    RETURNING id
                    """,
                    session_id,
                    actor,
                    event_type,
                    resource,
                    json.dumps(detail or {}),
                )

        row = await self._command(insert)
        return int(row["id"])
