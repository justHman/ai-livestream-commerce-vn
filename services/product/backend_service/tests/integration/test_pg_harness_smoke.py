"""Smoke test for the embedded-PostgreSQL test harness (B0).

Boots the portable PG server, connects, applies the real runtime schema, and
verifies a second database can be created/dropped. Failing here means the
harness itself is broken (binaries, initdb, or server start) — fix the harness
before writing any authoring integration test.
"""

from __future__ import annotations

import asyncpg
import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore

from .pg_harness import TestPostgres


@pytest.mark.asyncio
async def test_harness_connects_and_roundtrips(pg_url: str) -> None:
    conn = await asyncpg.connect(pg_url)
    try:
        value = await conn.fetchval("SELECT 1")
        assert value == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_harness_applies_real_runtime_schema(pg_url: str) -> None:
    store = PostgresRuntimeStore(pg_url)
    try:
        await store.connect()
        assert store.enabled is True
        await store.apply_schema()
        async with store._pool.acquire() as conn:  # noqa: SLF001 - harness smoke
            tables = {
                row["tablename"]
                for row in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
    finally:
        await store.close()
    assert {"sessions", "session_products", "entities", "audit_events"} <= tables


@pytest.mark.asyncio
async def test_harness_create_drop_database(pg_server: TestPostgres) -> None:
    dsn = await pg_server.create_database()
    assert dsn != pg_server.dsn("postgres")
    await pg_server.drop_database(dsn)
