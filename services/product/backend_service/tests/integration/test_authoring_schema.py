"""Authoring schema tests (Change B, B1): clean-DB migration + invariants.

RED before the authoring tables land in ``runtime_schema.sql``: the required
tables are absent. GREEN once the additive ``CREATE TABLE IF NOT EXISTS`` block
is appended and ``apply_schema()`` creates them all idempotently.
"""

from __future__ import annotations

import asyncpg
import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore

AUTHORING_TABLES = {
    "script_sets",
    "script_items",
    "product_script_plans",
    "script_segments",
    "script_versions",
    "script_gate_runs",
    "script_approvals",
    "script_generation_batches",
    "script_generation_jobs",
}


async def _tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row["tablename"] for row in rows}


async def _apply(pg_url: str) -> None:
    store = PostgresRuntimeStore(pg_url)
    try:
        await store.connect()
        await store.apply_schema()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_apply_schema_creates_all_authoring_tables(pg_url: str) -> None:
    await _apply(pg_url)
    conn = await asyncpg.connect(pg_url)
    try:
        tables = await _tables(conn)
    finally:
        await conn.close()
    missing = AUTHORING_TABLES - tables
    assert not missing, f"authoring tables missing after apply_schema: {sorted(missing)}"


@pytest.mark.asyncio
async def test_apply_schema_is_idempotent(pg_url: str) -> None:
    await _apply(pg_url)
    await _apply(pg_url)  # second apply must not error
    conn = await asyncpg.connect(pg_url)
    try:
        tables = await _tables(conn)
    finally:
        await conn.close()
    assert AUTHORING_TABLES <= tables


@pytest.mark.asyncio
async def test_script_items_unique_per_set_product(pg_url: str) -> None:
    await _apply(pg_url)
    conn = await asyncpg.connect(pg_url)
    try:
        await conn.execute(
            "INSERT INTO script_sets (id, shop_id, title) VALUES ('script_set:' || repeat('a',32), 'shop1', 't')"
        )
        await conn.execute(
            "INSERT INTO script_items (id, script_set_id, product_id) "
            "VALUES ('script_item:' || repeat('b',32), 'script_set:' || repeat('a',32), 'p1')"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO script_items (id, script_set_id, product_id) "
                "VALUES ('script_item:' || repeat('c',32), 'script_set:' || repeat('a',32), 'p1')"
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_script_versions_immutable_version_identity(pg_url: str) -> None:
    await _apply(pg_url)
    conn = await asyncpg.connect(pg_url)
    try:
        await conn.execute(
            "INSERT INTO script_sets (id, shop_id, title) VALUES ('script_set:' || repeat('a',32), 'shop1', 't')"
        )
        await conn.execute(
            "INSERT INTO script_items (id, script_set_id, product_id) "
            "VALUES ('script_item:' || repeat('b',32), 'script_set:' || repeat('a',32), 'p1')"
        )
        await conn.execute(
            "INSERT INTO script_versions (id, script_item_id, version, display_text, spoken_text, text_hash) "
            "VALUES ('script_version:' || repeat('d',32), 'script_item:' || repeat('b',32), 1, 'x', 'y', 'h1')"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO script_versions (id, script_item_id, version, display_text, spoken_text, text_hash) "
                "VALUES ('script_version:' || repeat('e',32), 'script_item:' || repeat('b',32), 1, 'x', 'y', 'h2')"
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_generation_job_idempotency_partial_unique(pg_url: str) -> None:
    await _apply(pg_url)
    conn = await asyncpg.connect(pg_url)
    try:
        await conn.execute(
            "INSERT INTO script_sets (id, shop_id, title) VALUES ('script_set:' || repeat('a',32), 'shop1', 't')"
        )
        await conn.execute(
            "INSERT INTO script_items (id, script_set_id, product_id) "
            "VALUES ('script_item:' || repeat('b',32), 'script_set:' || repeat('a',32), 'p1')"
        )
        await conn.execute(
            "INSERT INTO script_generation_batches (id, script_set_id, status) "
            "VALUES ('batch:' || repeat('f',32), 'script_set:' || repeat('a',32), 'queued')"
        )
        await conn.execute(
            "INSERT INTO script_generation_jobs "
            "(id, batch_id, script_item_id, product_id, intent, target_duration_s, idempotency_key) "
            "VALUES ('job:' || repeat('g',32), 'batch:' || repeat('f',32), "
            "'script_item:' || repeat('b',32), 'p1', 'generate_long_form', 600, 'key-1')"
        )
        # Same (item, intent, key) is rejected by the partial unique index.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO script_generation_jobs "
                "(id, batch_id, script_item_id, product_id, intent, target_duration_s, idempotency_key) "
                "VALUES ('job:' || repeat('h',32), 'batch:' || repeat('f',32), "
                "'script_item:' || repeat('b',32), 'p1', 'generate_long_form', 600, 'key-1')"
            )
        # Empty idempotency_key is exempt (partial index WHERE key <> '').
        await conn.execute(
            "INSERT INTO script_generation_jobs "
            "(id, batch_id, script_item_id, product_id, intent, target_duration_s, idempotency_key) "
            "VALUES ('job:' || repeat('i',32), 'batch:' || repeat('f',32), "
            "'script_item:' || repeat('b',32), 'p1', 'generate_long_form', 600, '')"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pointer_foreign_keys_present(pg_url: str) -> None:
    await _apply(pg_url)
    conn = await asyncpg.connect(pg_url)
    try:
        fks = {
            row["constraint_name"]
            for row in await conn.fetch(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE constraint_type = 'FOREIGN KEY' AND table_name LIKE 'script_%'"
            )
        }
    finally:
        await conn.close()
    assert "fk_item_current_version" in fks
    assert "fk_item_approved_version" in fks
    assert "fk_version_gate_run" in fks
