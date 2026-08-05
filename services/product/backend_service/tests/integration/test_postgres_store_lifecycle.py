"""Offline tests for PostgresRuntimeStore lifecycle + persist (fake asyncpg pool).

We never touch a real Postgres. A fake pool/conn records executed SQL + args
so we assert the right statements run with the right parameters.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

import backend.application.db.postgres_store as postgres_store
from backend.application.db.postgres_store import PostgresRuntimeStore, schema_path


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
        self.closed = False

    def acquire(self):
        return _FakeConn(self)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_connect_failure_leaves_no_pool_and_records_error(monkeypatch):
    async def create_pool(*args, **kwargs):
        raise TimeoutError("database unavailable")

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(create_pool=create_pool))
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")

    with pytest.raises(TimeoutError, match="database unavailable"):
        await store.connect()

    assert store._pool is None
    assert store.last_error == "TimeoutError: database unavailable"


@pytest.mark.asyncio
async def test_health_runs_bounded_select_one_and_reports_result():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()

    ok, error = await store.health()

    assert ok is True
    assert error is None
    assert any("SELECT 1" in sql for sql, _ in store._pool.statements)


@pytest.mark.asyncio
async def test_health_timeout_records_error(monkeypatch):
    class _SlowConn(_FakeConn):
        async def execute(self, sql, *args):
            await asyncio.sleep(10)

    class _SlowPool(_FakePool):
        def acquire(self):
            return _SlowConn(self)

    monkeypatch.setattr(postgres_store, "_COMMAND_TIMEOUT_SECONDS", 0.01)
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _SlowPool()

    ok, error = await store.health()

    assert ok is False
    assert error == "TimeoutError: "


@pytest.mark.asyncio
async def test_health_failure_records_error():
    class _BrokenConn(_FakeConn):
        async def execute(self, sql, *args):
            raise RuntimeError("connection lost")

    class _BrokenPool(_FakePool):
        def acquire(self):
            return _BrokenConn(self)

    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _BrokenPool()

    ok, error = await store.health()

    assert ok is False
    assert error == "RuntimeError: connection lost"
    assert store.last_error == error


@pytest.mark.asyncio
async def test_close_failure_records_error_after_pool_is_cleared():
    class _BrokenClosePool(_FakePool):
        async def close(self):
            raise RuntimeError("close failed")

    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _BrokenClosePool()

    with pytest.raises(RuntimeError, match="close failed"):
        await store.close()

    assert store._pool is None
    assert store.last_error == "RuntimeError: close failed"


@pytest.mark.asyncio
async def test_close_timeout_records_error_after_pool_is_cleared(monkeypatch):
    class _SlowClosePool(_FakePool):
        async def close(self):
            await asyncio.sleep(10)

    monkeypatch.setattr(postgres_store, "_COMMAND_TIMEOUT_SECONDS", 0.01)
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _SlowClosePool()

    with pytest.raises(TimeoutError):
        await store.close()

    assert store._pool is None
    assert store.last_error == "TimeoutError: "


@pytest.mark.asyncio
async def test_persistence_commands_use_bounded_timeout(monkeypatch):
    timeouts = []
    original_wait_for = postgres_store.asyncio.wait_for

    async def recording_wait_for(awaitable, *, timeout):
        timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(postgres_store.asyncio, "wait_for", recording_wait_for)
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()

    await store.upsert_session("sess-1")
    await store.get_session("sess-1")
    await store.insert_product_snapshot("sess-1", [{"id": "p-1", "name": "Product"}])
    await store.insert_viewer_msg("sess-1", "hello")
    await store.insert_director_decision("sess-1", "idle")
    await store.insert_audit_event("session.started", session_id="sess-1")

    assert timeouts == [postgres_store._COMMAND_TIMEOUT_SECONDS] * 6


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
