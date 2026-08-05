"""Unit tests for token auth, admin gating, debug gate, prod CORS (Task 7).

Covers:
  - BACKEND_API_TOKEN (viewer) dependency on /lite/* + /ws/control.
  - ADMIN_API_TOKEN dependency on /engines/* and /debug/*.
  - viewer token used on admin endpoint -> 403 (not 401).
  - missing/wrong token -> 401 (does not leak valid-vs-invalid).
  - DEBUG_ENABLED=0 -> /debug/* -> 404 (auth-gated so attackers can't probe).
  - APP_ENV=dev with no tokens -> auth DISABLED (existing tests still pass).
  - APP_ENV=prod + CORS_ORIGINS='*' -> create_app raises RuntimeError.
  - Finding 3: APP_ENV != 'dev' + CORS_ORIGINS='*' -> create_app raises
    (covers 'production', 'staging', 'prd' — anything not 'dev').

All tests offline (mock backend, no model loads).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import AppConfig
from conftest import make_deps as _Deps  # noqa: F401


# NOTE: ``create_app`` is imported lazily inside ``_client`` so that
# ``backend.main`` (and its module-level ``CONFIG = AppConfig.from_env()``)
# is first imported while the ``mock_env`` fixture has already set
# ``RENDER_BACKEND=mock``. A module-level import here would cache ``CONFIG``
# with ``render_backend="cloud"`` during collection, before any fixture runs,
# and break ``test_app_factory.py::test_create_app_default_*``.


# ---------- fixtures ----------


def _deps():
    return _Deps(
        
        
        
        director=None,
        
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
    from backend.main import create_app

    app = create_app(config=cfg, deps=_deps())
    return TestClient(app)


VIEWER = {"Authorization": "Bearer viewer-secret"}
ADMIN = {"Authorization": "Bearer admin-secret"}
WRONG = {"Authorization": "Bearer nope"}


# ---------- viewer auth on /lite/* ----------


def test_prod_missing_viewer_token_returns_401(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.post("/api/v1/sessions", json={})
    assert r.status_code == 401, r.text


def test_prod_wrong_viewer_token_returns_401(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.post("/api/v1/sessions", json={}, headers=WRONG)
    assert r.status_code == 401, r.text


def test_prod_valid_viewer_token_reaches_handler(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        r = client.post("/api/v1/sessions", json={}, headers=VIEWER)
    # Auth passed; handler runs. With mock backend, /sessions returns 200.
    assert r.status_code != 401, "auth should pass"
    assert r.status_code != 403, "viewer token is valid for /sessions"
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


def test_debug_routes_absent_regardless_of_token(mock_env: None) -> None:
    """Debug routes are not part of the production application (1.25)."""
    cfg = _prod_cfg(debug_enabled=True)
    with _client(cfg) as client:
        r = client.post(
            "/api/v1/debug/start",
            json={"session_id": "x"},
            headers=ADMIN,
        )
    assert r.status_code == 404, r.text


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
        r = client.post("/api/v1/sessions", json={})
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


def test_dev_debug_routes_still_absent(mock_env: None) -> None:
    """Debug routes are absent from the canonical app even in dev (1.25)."""
    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        debug_enabled=True,
    )
    with _client(cfg) as client:
        r = client.post("/api/v1/debug/start", json={"session_id": "x"})
    assert r.status_code == 404, r.text


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
    from backend.main import create_app

    with pytest.raises(RuntimeError, match=r"CORS_ORIGINS.*forbidden.*dev"):
        create_app(config=cfg, deps=_deps())


def test_nonprod_nondev_cors_star_raises_runtime_error(mock_env: None) -> None:
    """Finding 3: APP_ENV=production (or 'staging', 'prd', anything not 'dev')
    with CORS_ORIGINS='*' must raise. The old check only rejected 'prod';
    'production'/'staging' bypassed it and ran with wildcard CORS."""
    from backend.main import create_app

    for bad_env in ("production", "staging", "prd", "PROD", "Production"):
        cfg = AppConfig(
            render_backend="mock",
            app_env=bad_env,
            cors_origins="*",
            backend_api_token="x",
            admin_api_token="y",
        )
        with pytest.raises(RuntimeError, match=r"CORS_ORIGINS.*forbidden.*dev"):
            create_app(config=cfg, deps=_deps())


def test_dev_cors_star_allowed(mock_env: None) -> None:
    """dev + CORS='*' is allowed (default)."""
    from backend.main import create_app

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

