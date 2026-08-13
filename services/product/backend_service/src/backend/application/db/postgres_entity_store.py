"""Optional asyncpg entity store for Postgres (task 8.4).

Entity documents persist as JSONB under the ``entities`` table declared in
``runtime_schema.sql``; the JSON document semantics match the in-memory
adapter, so call sites never see the backend. asyncpg is an optional
dependency, imported only when DATABASE_URL is set.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from backend.application.entity.models import EntityDocument

_CONNECT_TIMEOUT_SECONDS = 5.0
_COMMAND_TIMEOUT_SECONDS = 5.0


class PostgresEntityRepository:
    """Thin asyncpg wrapper; no-op construction without a database URL.

    Shares the pool pattern of ``PostgresRuntimeStore`` but holds its own
    pool — runtime and entity stores connect independently. ``upsert``
    guards the revision invariant in SQL (same rule as the memory adapter).
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

    async def _command(self, operation):
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
            raise RuntimeError("PostgresEntityRepository not connected")
        return self._pool

    async def upsert(self, entity: EntityDocument) -> None:
        payload = json.dumps(entity.model_dump(mode="json"))

        async def upsert() -> None:
            async with self._require_pool().acquire() as conn:
                updated = await conn.execute(
                    """
                    INSERT INTO entities (entity_id, entity_type, revision, document, updated_at)
                    VALUES ($1, $2, $3, $4::jsonb, NOW())
                    ON CONFLICT (entity_id) DO UPDATE SET
                        entity_type = EXCLUDED.entity_type,
                        revision = EXCLUDED.revision,
                        document = EXCLUDED.document,
                        updated_at = NOW()
                    WHERE entities.revision < EXCLUDED.revision
                    """,
                    entity.id,
                    entity.entity_type,
                    entity.revision,
                    payload,
                )
                if updated == "UPDATE 0":
                    raise ValueError(
                        f"entity {entity.id}: revision {entity.revision} <= stored revision"
                    )

        await self._command(upsert)

    async def get(self, entity_id: str) -> Optional[EntityDocument]:
        async def get() -> any:  # noqa: F821
            async with self._require_pool().acquire() as conn:
                return await conn.fetchrow(
                    "SELECT document FROM entities WHERE entity_id = $1", entity_id
                )

        row = await self._command(get)
        return EntityDocument.model_validate(json.loads(row["document"])) if row else None

    async def delete(self, entity_id: str) -> bool:
        async def delete() -> any:  # noqa: F821
            async with self._require_pool().acquire() as conn:
                return await conn.execute("DELETE FROM entities WHERE entity_id = $1", entity_id)

        status = await self._command(delete)
        return status.startswith("DELETE 1")

    async def list_entities(self, entity_type: Optional[str] = None) -> list[EntityDocument]:
        async def list_rows() -> any:  # noqa: F821
            async with self._require_pool().acquire() as conn:
                if entity_type is None:
                    return await conn.fetch("SELECT document FROM entities ORDER BY entity_id")
                return await conn.fetch(
                    "SELECT document FROM entities WHERE entity_type = $1 ORDER BY entity_id",
                    entity_type,
                )

        rows = await self._command(list_rows)
        return [EntityDocument.model_validate(json.loads(row["document"])) for row in rows]
