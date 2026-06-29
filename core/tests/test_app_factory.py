"""Unit tests for the FastAPI app factory + split health endpoints (Task 6).

Verifies:
  - ``create_app()`` (default, env-driven) returns a FastAPI app with the
    expected title and works under ``RENDER_BACKEND=mock`` + empty
    ``LIVEAVATAR_API_KEY``.
  - ``create_app(deps=...)`` accepts injected dependencies (no heavy model
    load) and wires them into the router.
  - ``/health/live`` is always 200 with ``{"ok": True, "status": "live"}``.
  - ``/health/ready`` reports backend readiness (200) with the active
    ``render_backend`` / ``llm_engine`` / ``tts_engine`` fields.
  - Importing ``core.server`` with mock + empty key does NOT raise and
    exposes a module-level ``app`` (FastAPI) + ``create_app`` callable.
  - The existing ``/health`` route is preserved.

All tests offline — no ``LIVEAVATAR_API_KEY``, no ``REDIS_URL``, no GPU, no
model downloads. Uses ``fastapi.testclient.TestClient`` for real HTTP calls.

Note: a pre-existing ``StarletteDeprecationWarning`` about ``httpx`` is
emitted by ``fastapi.testclient`` on this FastAPI/Starlette combo. It is
infrastructure noise, not a test failure.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.engine_manager import EngineManager
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore


# ---------- fixtures ----------


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env that lets the module-level app boot without a cloud key."""
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")


@pytest.fixture
def injected_deps() -> v1.V1Deps:
    """A fully-injected V1Deps using the mock backend + an empty EngineManager."""
    backend = MockRenderBackend()
    store = InMemorySessionStore()
    hub = v1.ControlHub()
    engine_mgr = EngineManager()  # no llm/tts loaded
    return v1.V1Deps(
        backend=backend,
        store=store,
        hub=hub,
        director=None,
        engine_manager=engine_mgr,
    )


# ---------- create_app() default (env-driven) ----------


def test_create_app_default_returns_fastapi_with_expected_title(mock_env: None) -> None:
    from core.server import create_app

    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.title == "VN Live-Commerce Host — core API"


def test_module_level_app_is_fastapi_and_create_app_callable(mock_env: None) -> None:
    """Importing core.server must not raise; module-level `app` must exist."""
    import importlib

    import core.server as server_mod

    # Re-import under the mock env to make sure the module-level app was built
    # against RENDER_BACKEND=mock (idempotent if already imported).
    importlib.reload(server_mod)
    assert isinstance(server_mod.app, FastAPI)
    assert callable(server_mod.create_app)


# ---------- /health/live + /health/ready ----------


def test_health_live_always_200(mock_env: None, injected_deps: v1.V1Deps) -> None:
    from core.server import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/live")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "live"


def test_health_ready_200_mock_backend(mock_env: None, injected_deps: v1.V1Deps) -> None:
    from core.server import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["render_backend"] == "mock"
    # No llm/tts loaded on the injected EngineManager -> "none" / "tone" stubs.
    assert body["llm_engine"] in ("none", None, "")
    assert body["tts_engine"] in ("tone", None, "")


def test_health_existing_route_preserved(mock_env: None, injected_deps: v1.V1Deps) -> None:
    from core.server import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["render_backend"] == "mock"


# ---------- root route ----------


def test_root_route_returns_render_backend(mock_env: None, injected_deps: v1.V1Deps) -> None:
    from core.server import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "vn-live-commerce-host"
    assert body["render_backend"] == "mock"
    assert body["engine_manager"] is True


# ---------- injected deps skip model loading ----------


def test_injected_deps_engine_manager_used_as_is(
    mock_env: None, injected_deps: v1.V1Deps
) -> None:
    """create_app(deps=...) must NOT call engine_mgr.load_llm/load_tts.

    We assert by inspecting the injected EngineManager after boot: it should
    still have no llm/tts loaded (``engine_manager.llm is None`` and
    ``engine_manager.tts is None``). If create_app had loaded models, those
    attributes would be non-None (or the call would have failed offline).
    """
    from core.server import create_app

    app = create_app(deps=injected_deps)
    em: EngineManager = injected_deps.engine_manager  # type: ignore[assignment]
    assert em.llm is None, "create_app(deps=...) must not load an LLM engine"
    assert em.tts is None, "create_app(deps=...) must not load a TTS engine"
    # Sanity: the app is still a FastAPI app.
    assert isinstance(app, FastAPI)


def test_health_ready_reports_llm_none_when_not_loaded(
    mock_env: None, injected_deps: v1.V1Deps
) -> None:
    """/health/ready on injected (no-model) deps reports llm_engine 'none'."""
    from core.server import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    body = r.json()
    assert r.status_code == 200
    assert body["llm_engine"] == "none"
    assert body["tts_engine"] == "tone"
    assert body["ok"] is True
