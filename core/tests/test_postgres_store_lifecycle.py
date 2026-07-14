"""Offline tests for PostgresRuntimeStore lifecycle + persist (fake asyncpg pool).

We never touch a real Postgres. A fake pool/conn records executed SQL + args
so we assert the right statements run with the right parameters.
"""

from __future__ import annotations

import pytest

from core.db.postgres_store import PostgresRuntimeStore, schema_path


class _FakeConn:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def execute(self, sql, *args):
        self._pool.statements.append((sql, args))

    async def fetchrow(self, sql, *args):
        self._pool.statements.append((sql, args))
        return {"id": 1}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self) -> None:
        self.statements: list = []

    def acquire(self):
        return _FakeConn(self)


@pytest.mark.asyncio
async def test_apply_schema_runs_schema_sql():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()
    await store.apply_schema()
    sql = store._pool.statements[0][0]
    assert "CREATE TABLE" in sql
    assert "sessions" in sql


@pytest.mark.asyncio
async def test_insert_product_snapshot_upserts_rows():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()
    products = [
        {"id": "P001", "name": "Kem chong nang", "price": 329000, "features": ["SPF50"]},
        {"id": "P002", "name": "Sua rua mat", "price": 159000, "features": []},
    ]
    await store.insert_product_snapshot("sess-1", products)
    sqls = [s[0] for s in store._pool.statements]
    assert any("session_products" in s for s in sqls)
    inserts = [s for s in store._pool.statements if "INSERT" in s[0].upper()]
    assert len(inserts) >= 2


@pytest.mark.asyncio
async def test_insert_viewer_msg_returns_id():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()
    rid = await store.insert_viewer_msg("sess-1", "gia bao nhieu", author="v1")
    assert rid == 1


@pytest.mark.asyncio
async def test_insert_director_decision_returns_id():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()
    rid = await store.insert_director_decision(
        "sess-1",
        "answer_cluster",
        product_id="P001",
        score=0.8,
        phase="selling",
        utterance="Kem nay SPF50 nhe",
        reason="cluster match",
    )
    assert rid == 1


def test_schema_path_resolves():
    p = schema_path()
    assert p.is_file()
    assert p.name == "runtime_schema.sql"
