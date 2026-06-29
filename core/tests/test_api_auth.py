"""Unit tests for token auth, admin gating, debug gate, prod CORS (Task 7).

Covers:
  - BACKEND_API_TOKEN (viewer) dependency on /lite/* + /ws/control.
  - ADMIN_API_TOKEN dependency on /engines/* and /debug/*.
  - viewer token used on admin endpoint -> 403 (not 401).
  - missing/wrong token -> 401 (does not leak valid-vs-invalid).
  - DEBUG_ENABLED=0 -> /debug/* -> 404 (auth-gated so attackers can't probe).
  - APP_ENV=dev with no tokens -> auth DISABLED (existing tests still pass).
  - APP_ENV=prod + CORS_ORIGINS='*' -> create_app raises RuntimeError.

All tests offline (mock backend, no model loads).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.engine_manager import EngineManager
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore


# NOTE: ``create_app`` is imported lazily inside ``_client`` so that
# ``core.server`` (and its module-level ``CONFIG = AppConfig.from_env()``)
# is first imported while the ``mock_env`` fixture has already set
# ``RENDER_BACKEND=mock``. A module-level import here would cache ``CONFIG``
# with ``render_backend="cloud"`` during collection, before any fixture runs,
# and break ``test_app_factory.py::test_create_app_default_*``.


# ---------- fixtures ----------


def _deps() -> v1.V1Deps:
    return v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        director=None,
        engine_manager=EngineManager(),
    )


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")


def _prod_cfg(
    backend_token: str = "viewer-secret",
    admin_token: str = "admin-secret",
    debug_enabled: bool = True,
    cors_origins: str = "https://example.com",
) -> AppConfig:
    return AppConfig(
        render_backend="mock",
        app_env="prod",
        backend_api_token=backend_token,
        admin_api_token=admin_token,
        debug_enabled=debug_enabled,
        cors_origins=cors_origins,
    )


def _client(cfg: AppConfig) -> TestClient:
    from core.server import create_app

    app = create_app(config=cfg, deps=_deps())
    return TestClient(app)


VIEWER = {"Authorization": "Bearer viewer-secret"}
ADMIN = {"Authorization": "Bearer admin-secret"}
WRONG = {"Authorization": "Bearer nope"}


# ---------- viewer auth on /lite/* ----------


def test_prod_missing_viewer_token_returns_401(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.post("/api/v1/lite/start", json={})
    assert r.status_code == 401, r.text


def test_prod_wrong_viewer_token_returns_401(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.post("/api/v1/lite/start", json={}, headers=WRONG)
    assert r.status_code == 401, r.text


def test_prod_valid_viewer_token_reaches_handler(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.post("/api/v1/lite/start", json={}, headers=VIEWER)
    # Auth passed; handler runs. With mock backend, /lite/start returns 200.
    assert r.status_code != 401, "auth should pass"
    assert r.status_code != 403, "viewer token is valid for /lite/*"
    # Mock backend start() returns 200 with session_id.
    assert r.status_code == 200, r.text


# ---------- admin auth on /engines/* ----------


def test_prod_missing_admin_token_returns_401(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.get("/api/v1/engines")
    assert r.status_code == 401, r.text


def test_prod_wrong_admin_token_returns_401(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.get("/api/v1/engines", headers=WRONG)
    assert r.status_code == 401, r.text


def test_prod_viewer_token_on_admin_returns_403(mock_env: None) -> None:
    """A viewer token must NOT satisfy admin endpoints -> 403, not 401."""
    with _client(_prod_cfg()) as client:
        r = client.get("/api/v1/engines", headers=VIEWER)
    assert r.status_code == 403, r.text


def test_prod_valid_admin_token_reaches_handler(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.get("/api/v1/engines", headers=ADMIN)
    assert r.status_code != 401, "admin auth should pass"
    assert r.status_code != 403, "admin token is valid"
    # EngineManager.status() returns 200.
    assert r.status_code == 200, r.text


# ---------- debug gate ----------


def test_debug_disabled_returns_404_regardless_of_token(mock_env: None) -> None:
    cfg = _prod_cfg(debug_enabled=False)
    with _client(cfg) as client:
        # Even with a valid admin token, debug disabled -> 404.
        r = client.post(
            "/api/v1/debug/start",
            json={"session_id": "x"},
            headers=ADMIN,
        )
    assert r.status_code == 404, r.text


def test_debug_disabled_404_before_auth_leak(mock_env: None) -> None:
    """No token + debug disabled -> 404 (not 401). Debug gate runs first."""
    cfg = _prod_cfg(debug_enabled=False)
    with _client(cfg) as client:
        r = client.post("/api/v1/debug/start", json={"session_id": "x"})
    assert r.status_code == 404, r.text


def test_debug_enabled_admin_token_reaches_handler(mock_env: None) -> None:
    cfg = _prod_cfg(debug_enabled=True)
    with _client(cfg) as client:
        r = client.post(
            "/api/v1/debug/start",
            json={"session_id": "x"},
            headers=ADMIN,
        )
    # Auth passed + debug enabled. Handler runs; Director is None -> 501.
    assert r.status_code not in (401, 403, 404), r.text
    assert r.status_code == 501, r.text  # Director not enabled


# ---------- dev mode: auth disabled with empty tokens ----------


def test_dev_no_tokens_disables_viewer_auth(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        debug_enabled=True,
    )
    with _client(cfg) as client:
        r = client.post("/api/v1/lite/start", json={})
    assert r.status_code != 401, "dev+no tokens -> auth disabled"
    assert r.status_code == 200, r.text


def test_dev_no_tokens_disables_admin_auth(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        debug_enabled=True,
    )
    with _client(cfg) as client:
        r = client.get("/api/v1/engines")
    assert r.status_code not in (401, 403), "dev+no tokens -> admin auth disabled"
    assert r.status_code == 200, r.text


def test_dev_no_tokens_disables_debug_auth(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        debug_enabled=True,
    )
    with _client(cfg) as client:
        r = client.post("/api/v1/debug/start", json={"session_id": "x"})
    assert r.status_code not in (401, 403, 404), r.text
    # Director not enabled -> 501 from handler.
    assert r.status_code == 501, r.text


# ---------- prod admin token empty -> admin always 401 ----------


def test_prod_empty_admin_token_rejects_admin(mock_env: None) -> None:
    """In prod, if ADMIN_API_TOKEN is empty, admin endpoints always 401."""
    cfg = _prod_cfg(admin_token="")
    with _client(cfg) as client:
        r = client.get("/api/v1/engines")
    assert r.status_code == 401, r.text


# ---------- prod CORS validation ----------


def test_prod_cors_star_raises_runtime_error(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="prod",
        cors_origins="*",
        backend_api_token="x",
        admin_api_token="y",
    )
    from core.server import create_app

    with pytest.raises(RuntimeError, match=r"CORS_ORIGINS.*forbidden.*prod"):
        create_app(config=cfg, deps=_deps())


def test_dev_cors_star_allowed(mock_env: None) -> None:
    """dev + CORS='*' is allowed (default)."""
    from core.server import create_app

    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        cors_origins="*",
        backend_api_token="",
        admin_api_token="",
    )
    app = create_app(config=cfg, deps=_deps())
    assert isinstance(app, FastAPI)


# ---------- health endpoints stay public ----------


def test_health_routes_are_public_in_prod(mock_env: None) -> None:
    """Health endpoints must NOT require auth even in prod."""
    with _client(_prod_cfg()) as client:
        for path in ("/api/v1/health", "/api/v1/health/live", "/api/v1/health/ready"):
            r = client.get(path)
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"
