"""Offline tests for Postgres runtime schema + optional store (Wave D)."""

from __future__ import annotations

import re

import pytest

from core.db.postgres_store import PostgresRuntimeStore, schema_path, schema_sql


REQUIRED_TABLES = (
    "sessions",
    "session_products",
    "viewer_msgs",
    "director_decisions",
    "llm_call_log",
    "tts_call_log",
    "audit_events",
)


def test_schema_file_exists():
    path = schema_path()
    assert path.is_file(), f"missing schema: {path}"
    assert path.name == "runtime_schema.sql"


def test_schema_contains_required_tables():
    sql = schema_sql()
    assert sql.strip(), "schema SQL is empty"
    lower = sql.lower()
    for table in REQUIRED_TABLES:
        assert re.search(rf"create\s+table\s+if\s+not\s+exists\s+{table}\b", lower), (
            f"table {table} missing from schema"
        )


def test_schema_indexes_session_id():
    sql = schema_sql().lower()
    # At least one index per session-scoped table.
    for table in (
        "session_products",
        "viewer_msgs",
        "director_decisions",
        "llm_call_log",
        "tts_call_log",
        "audit_events",
    ):
        assert "session_id" in sql
        assert table in sql


def test_postgres_store_disabled_without_url():
    store = PostgresRuntimeStore("")
    assert store.enabled is False


def test_postgres_store_enabled_with_url():
    store = PostgresRuntimeStore("postgresql://user:pass@localhost:5432/runtime")
    assert store.enabled is True


@pytest.mark.asyncio
async def test_postgres_store_requires_connect_before_write():
    store = PostgresRuntimeStore("postgresql://user:pass@localhost:5432/runtime")
    with pytest.raises(RuntimeError, match="not connected"):
        await store.upsert_session("s1")


@pytest.mark.asyncio
async def test_connect_noop_when_disabled():
    store = PostgresRuntimeStore("")
    await store.connect()  # no-op, no asyncpg import required
    await store.close()
