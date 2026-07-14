"""Server wires PostgresRuntimeStore lifecycle when DATABASE_URL is set.

Uses a fake store injected via V1Deps so no real DB is touched. Asserts
connect/apply_schema fire on startup and close on shutdown.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.db.postgres_store import PostgresRuntimeStore
from core.server import create_app


class _FakePgStore:
    def __init__(self) -> None:
        self.enabled = True
        self.connected = False
        self.schema_applied = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def apply_schema(self):
        self.schema_applied = True

    async def close(self):
        self.closed = True


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
