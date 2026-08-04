"""Server wires PostgresRuntimeStore lifecycle when DATABASE_URL is set.

Uses a fake store injected via V1Deps so no real DB is touched. Asserts
connect/apply_schema fire on startup and close on shutdown.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.db.postgres_store import PostgresRuntimeStore
from backend.bootstrap import create_app, create_container
from backend.bootstrap import lifespan as server

def _container_for(*, backend=None, store=None, hub=None, orchestrators=None, coordinator=None, pg_store=None, config=None):
    """Build a BootstrapContainer mirroring the legacy V1Deps fields."""
    from core.render.mock import MockRenderBackend
    from core.store import InMemorySessionStore
    container = create_container(
        backend=backend if backend is not None else MockRenderBackend(),
        store=store if store is not None else InMemorySessionStore(),
        config=config or AppConfig(render_backend="mock", app_env="dev"),
        coordinator=coordinator,
        pg_store=pg_store,
    )
    if orchestrators is not None:
        container.orchestrators = orchestrators
    return container


class _FakePgStore:
    def __init__(self, failures: int = 0) -> None:
        self.enabled = True
        self.failures = failures
        self.connect_calls = 0
        self._pool = None
        self.connected = False
        self.schema_applied = False
        self.closed = False
        self.close_calls = 0
        self.last_error = None

    async def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self.failures:
            self.last_error = "RuntimeError: database unavailable"
            raise RuntimeError("database unavailable")
        self.connected = True
        self._pool = object()
        self.last_error = None

    async def apply_schema(self):
        self.schema_applied = True

    async def health(self):
        return self.connected, self.last_error

    async def close(self):
        self.close_calls += 1
        self.closed = True
        self.connected = False
        self._pool = None


class _UnavailablePgStore(_FakePgStore):
    def __init__(self) -> None:
        super().__init__(failures=3)

    async def health(self):
        return False, self.last_error


class _SchemaFailurePgStore(_FakePgStore):
    async def apply_schema(self):
        self.schema_applied = True
        raise RuntimeError("schema unavailable")


class _HealthFailurePgStore(_FakePgStore):
    async def health(self):
        raise RuntimeError("database secret marker")


class _RecordingBackend:
    name = "recording"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def stop_all(self) -> None:
        self.events.append("backend")


class _RecordingCoordinator:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def stop_all(self) -> None:
        self.events.append("coordinator")


class _RecordingOrchestrator:
    async def cancel(self, session_id):
        self.events.append(f"orchestrator:{session_id}")

    def __init__(self, events: list[str]) -> None:
        self.events = events


class _OrderedPgStore(_FakePgStore):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def close(self):
        self.events.append("postgres")
        await super().close()


class _AsyncCloseBackend(_RecordingBackend):
    async def close(self) -> None:
        self.events.append("close")


class _BrokenBackend(_RecordingBackend):
    def stop_all(self) -> None:
        self.events.append("backend")
        raise RuntimeError("render teardown failed")


class _BrokenOrchestrator:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def cancel(self, session_id) -> None:
        self.events.append(f"orchestrator:{session_id}")
        raise RuntimeError("orchestrator teardown failed")


class _SlowOrchestrator:
    async def cancel(self, session_id):
        await asyncio.Event().wait()


def _mock_env(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/runtime")


def test_app_lifecycle_connects_and_closes_pg_store(monkeypatch):
    _mock_env(monkeypatch)
    config = AppConfig.from_env()
    assert config.database_url != ""

    pg = _FakePgStore()
    backend = config.build_render_backend()
    deps = v1.V1Deps(
        backend=backend,
        store=config.build_store(),
        hub=v1.ControlHub(),
        director=None,
        engine_manager=None,
        config=config,
        locks=None,
        orchestrators={},
        coordinator=None,
        pg_store=pg,
    )
    app = create_app(config=config, deps=deps)

    with TestClient(app) as client:
        assert pg.connected is True
        assert pg.schema_applied is True
        # Server still serves health while pg is connected.
        r = client.get("/api/v1/health/live")
        assert r.status_code == 200
    # After the with-block, lifespan shutdown ran.
    assert pg.closed is True


def test_startup_retries_pg_exactly_three_times(monkeypatch):
    _mock_env(monkeypatch)
    config = AppConfig.from_env()
    pg = _FakePgStore(failures=2)
    deps = v1.V1Deps(
        backend=config.build_render_backend(),
        store=config.build_store(),
        hub=v1.ControlHub(),
        config=config,
        pg_store=pg,
    )

    with TestClient(create_app(config=config, deps=deps)):
        assert pg.connect_calls == 3
        assert pg.schema_applied is True


@pytest.mark.asyncio
async def test_schema_failure_retries_with_exact_backoff_and_closes_store(monkeypatch):
    pg = _SchemaFailurePgStore()
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(server.asyncio, "sleep", sleep)

    await server._connect_postgres(_container_for(pg_store=pg))

    assert pg.connect_calls == 3
    assert pg.close_calls == 3
    assert pg.closed is True
    assert pg._pool is None
    assert delays == [0.1, 0.2]


@pytest.mark.asyncio
async def test_startup_cancellation_propagates_without_retry(monkeypatch):
    pg = _FakePgStore(failures=1)

    async def cancelled_sleep(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(server.asyncio, "sleep", cancelled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await server._connect_postgres(_container_for(pg_store=pg))

    assert pg.connect_calls == 1


def test_ready_is_false_after_configured_pg_startup_failure(monkeypatch):
    _mock_env(monkeypatch)
    config = AppConfig.from_env()
    pg = _UnavailablePgStore()
    deps = v1.V1Deps(
        backend=config.build_render_backend(),
        store=config.build_store(),
        hub=v1.ControlHub(),
        config=config,
        pg_store=pg,
    )

    with TestClient(create_app(config=config, deps=deps)) as client:
        response = client.get("/api/v1/health/ready")

    assert pg.connect_calls == 3
    assert response.json()["ok"] is False
    assert response.json()["postgres_error"] == "RuntimeError: database unavailable"


def test_ready_handles_pg_health_exception_without_details(monkeypatch, caplog):
    _mock_env(monkeypatch)
    config = AppConfig.from_env()
    pg = _HealthFailurePgStore()
    deps = v1.V1Deps(
        backend=config.build_render_backend(),
        store=config.build_store(),
        hub=v1.ControlHub(),
        config=config,
        pg_store=pg,
    )

    with caplog.at_level(logging.WARNING, logger="core.api.v1"):
        with TestClient(create_app(config=config, deps=deps)) as client:
            response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["postgres"] == "not_ready"
    assert response.json()["postgres_error"] == "RuntimeError"
    assert "database secret marker" not in caplog.text


def test_shutdown_cleans_orchestrators_before_backend_and_postgres(monkeypatch):
    _mock_env(monkeypatch)
    config = AppConfig.from_env()
    events: list[str] = []
    pg = _OrderedPgStore(events)
    orchestrator = _RecordingOrchestrator(events)
    deps = v1.V1Deps(
        backend=_RecordingBackend(events),
        store=config.build_store(),
        hub=v1.ControlHub(),
        config=config,
        orchestrators={"session-1": {"orchestrator": orchestrator}},
        coordinator=_RecordingCoordinator(events),
        pg_store=pg,
    )

    with TestClient(create_app(config=config, deps=deps)):
        pass

    assert events == ["orchestrator:session-1", "coordinator", "backend", "postgres"]


@pytest.mark.asyncio
async def test_shutdown_backend_failure_still_closes_postgres(caplog):
    events: list[str] = []
    deps = v1.V1Deps(
        backend=_BrokenBackend(events),
        store=None,
        hub=v1.ControlHub(),
    )
    pg = _OrderedPgStore(events)
    container = _container_for(backend=deps.backend, hub=deps.hub, pg_store=pg)

    with caplog.at_level(logging.ERROR, logger="backend.bootstrap.lifespan"):
        await server._shutdown(container)

    assert events == ["backend", "postgres"]
    assert "stage=render.stop_all error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_orchestrator_failure_continues_all_stages(caplog):
    events: list[str] = []
    deps = v1.V1Deps(
        backend=_RecordingBackend(events),
        store=None,
        hub=v1.ControlHub(),
        orchestrators={"session-1": {"orchestrator": _BrokenOrchestrator(events)}},
        coordinator=_RecordingCoordinator(events),
    )
    pg = _OrderedPgStore(events)
    container = _container_for(
        backend=deps.backend, hub=deps.hub, orchestrators=deps.orchestrators, coordinator=deps.coordinator, pg_store=pg
    )

    with caplog.at_level(logging.ERROR, logger="backend.bootstrap.lifespan"):
        await server._shutdown(container)

    assert events == ["orchestrator:session-1", "coordinator", "backend", "postgres"]
    assert "stage=orchestrators error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_timeout_logs_unfinished_stage_and_cleans_task(monkeypatch, caplog):
    events: list[str] = []
    deps = v1.V1Deps(
        backend=_RecordingBackend(events),
        store=None,
        hub=v1.ControlHub(),
        orchestrators={"session-1": {"orchestrator": _SlowOrchestrator()}},
    )
    monkeypatch.setattr(server, "_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    before = set(asyncio.all_tasks())
    container = _container_for(hub=deps.hub, orchestrators=deps.orchestrators)

    with caplog.at_level(logging.ERROR, logger="backend.bootstrap.lifespan"):
        await server._shutdown(container)

    assert "stage timed out stage=orchestrators" in caplog.text
    assert set(asyncio.all_tasks()) == before


@pytest.mark.asyncio
async def test_shutdown_cancellation_awaits_cleanup_task():
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingOrchestrator:
        async def cancel(self, session_id) -> None:
            started.set()
            await release.wait()

    deps = v1.V1Deps(
        backend=_RecordingBackend([]),
        store=None,
        hub=v1.ControlHub(),
        orchestrators={"session-1": {"orchestrator": _BlockingOrchestrator()}},
    )
    before = set(asyncio.all_tasks())
    container = _container_for(backend=deps.backend, hub=deps.hub, orchestrators=deps.orchestrators)
    shutdown = asyncio.create_task(server._shutdown(container))
    await started.wait()
    shutdown.cancel()

    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert set(asyncio.all_tasks()) == before


@pytest.mark.asyncio
async def test_shutdown_awaits_async_backend_close():
    events: list[str] = []
    deps = v1.V1Deps(
        backend=_AsyncCloseBackend(events),
        store=None,
        hub=v1.ControlHub(),
    )
    container = _container_for(backend=deps.backend, hub=deps.hub)

    await server._shutdown(container)

    assert events == ["backend", "close"]


def test_app_lifecycle_skips_pg_when_no_database_url(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    config = AppConfig.from_env()
    pg = PostgresRuntimeStore(config.database_url)
    assert pg.enabled is False
