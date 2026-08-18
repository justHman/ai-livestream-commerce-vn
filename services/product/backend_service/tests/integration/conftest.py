"""Shared fixtures for backend integration tests (OpenSpec 1.50/1.51).

Constructs isolated apps from explicit test settings (no collection-time
env mutation). ``make_deps`` builds the legacy deps-shaped object that
``create_app(deps=...)`` mirrors into the typed BootstrapContainer.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator

import pytest

from backend.api.v1.hub import AvatarStore, ControlHub
from backend.application.db import InMemorySessionStore
from backend.application.render.mock import MockRenderBackend
from backend.config import AppConfig
from backend.engine_manager import EngineManager

from .pg_harness import TestPostgres


def make_deps(
    *,
    backend: Any = None,
    store: Any = None,
    hub: Any = None,
    config: AppConfig | None = None,
    director: Any = None,
    engine_manager: Any = None,
    coordinator: Any = None,
    pg_store: Any = None,
    livekit_publishers: Any = None,
    avatars: Any = None,
    orchestrators: dict | None = None,
    entity_repo: Any = None,
):
    """Build the deps-shaped object accepted by ``create_app(deps=...)``."""

    class _Deps:
        def __init__(self) -> None:
            self.backend = backend if backend is not None else MockRenderBackend()
            self.store = store if store is not None else InMemorySessionStore()
            self.hub = hub if hub is not None else ControlHub()
            self.config = config
            self.director = director
            self.engine_manager = engine_manager if engine_manager is not None else EngineManager()
            self.coordinator = coordinator
            self.pg_store = pg_store
            self.livekit_publishers = livekit_publishers
            self.avatars = avatars if avatars is not None else AvatarStore()
            self.locks = None
            self.orchestrators = orchestrators if orchestrators is not None else {}
            self.entity_repo = entity_repo

    return _Deps()


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline-safe per-test env (fixture-scoped, never at collection)."""
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")


# ── embedded PostgreSQL (portable binaries, no Docker) ──────────────────────


@pytest.fixture(scope="session")
def pg_server() -> Iterator[TestPostgres]:
    """One ephemeral PostgreSQL server for the whole integration session."""
    server = TestPostgres()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
async def pg_url(pg_server: TestPostgres) -> AsyncIterator[str]:
    """Isolated database per test; drops it on teardown.

    Async so create/drop never need ``asyncio.run()`` inside a pytest-asyncio
    test (which would conflict with the running event loop).
    """
    dsn = await pg_server.create_database()
    try:
        yield dsn
    finally:
        await pg_server.drop_database(dsn)
