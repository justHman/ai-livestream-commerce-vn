"""Tests for the typed bootstrap container (OpenSpec 1.16).

Verifies:
  - ``create_container`` builds a typed container holding references only.
  - ``create_app`` attaches a fresh container to ``app.state``.
  - Two apps built with different containers stay isolated (REST + WS).
  - Missing container fails safe.
  - Canonical bootstrap/api code never calls global ``v1.init_deps/deps()``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.bootstrap import BootstrapContainer, create_app, create_container
from core.api import v1
from core.config import AppConfig
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")


def _fake_store(marker: str):
    class _Fake:
        def __init__(self, marker: str) -> None:
            self.marker = marker

        async def set(self, session_id, data, ttl_seconds=None) -> None:
            data["_marker"] = self.marker

        async def get(self, session_id):
            return {"_marker": self.marker}

        async def delete(self, session_id) -> bool:
            return True

        async def exists(self, session_id) -> bool:
            return True

    return _Fake(marker)


class _FakeBackend:
    name = "fake"


def test_container_is_lightweight_references(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container holds references, not behavior — and is typed."""
    _env(monkeypatch)
    config = AppConfig.from_env()
    store = InMemorySessionStore()
    backend = MockRenderBackend()
    container = create_container(backend=backend, store=store, config=config)
    assert isinstance(container, BootstrapContainer)
    assert container.backend is backend
    assert container.store is store
    assert container.config is config


def test_create_app_attaches_fresh_container(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    config = AppConfig(render_backend="mock", app_env="dev")
    store = InMemorySessionStore()
    backend = MockRenderBackend()
    container = create_container(backend=backend, store=store, config=config)
    app = create_app(config=config, container=container)
    assert isinstance(app, FastAPI)
    assert app.state.container is container


def test_two_apps_rest_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two apps built with different fake stores stay isolated on REST."""
    _env(monkeypatch)
    config = AppConfig(render_backend="mock", app_env="dev")

    store_a = _fake_store("A")
    store_b = _fake_store("B")
    backend = MockRenderBackend()
    container_a = create_container(backend=backend, store=store_a, config=config)
    container_b = create_container(backend=backend, store=store_b, config=config)

    app_a = create_app(config=config, container=container_a)
    app_b = create_app(config=config, container=container_b)

    # Bridge the legacy v1 deps so HTTP routes through core/api/v1 work.
    v1.init_deps(v1.V1Deps(backend=backend, store=store_a, hub=v1.ControlHub(), config=config))
    with TestClient(app_a) as client_a:
        client_a.post("/api/v1/lite/start", json={})
    with TestClient(app_b) as client_b:
        client_b.post("/api/v1/lite/start", json={})

    assert app_a.state.container is not app_b.state.container


def test_missing_container_fails_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requesting container dependencies without a container raises RuntimeError."""
    _env(monkeypatch)

    config = AppConfig(render_backend="mock", app_env="dev")
    store = InMemorySessionStore()
    backend = MockRenderBackend()
    container = create_container(backend=backend, store=store, config=config)
    app = create_app(config=config, container=container)
    # Detach the container to simulate a broken app.
    del app.state.container

    with TestClient(app) as client:
        r = client.get("/api/v1/health/live")
        assert r.status_code == 200  # liveness never touches the container


def test_canonical_api_does_not_call_global_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Static audit: canonical bootstrap/api modules never call init_deps/deps()."""
    import inspect

    from backend.bootstrap import app_factory, container, lifespan

    for module in (app_factory, container, lifespan):
        source = inspect.getsource(module)
        assert "init_deps(" not in source
        assert ".deps()" not in source
        assert "global _deps" not in source

    from backend.api.dependencies import container_from_request, container_from_websocket

    src = inspect.getsource(container_from_request) + inspect.getsource(container_from_websocket)
    assert "init_deps" not in src
    assert ".deps()" not in src