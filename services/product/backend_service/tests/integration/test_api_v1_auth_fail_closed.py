"""Fail-closed auth on REAL protected /api/v1 routes (R8.9 / BLOCKER 4).

Canonical v1 routes depend on ``backend.api.v1.auth`` (``viewer_auth`` /
``admin_auth`` / ``validate_ws_token``). Those dependencies MUST fail closed
exactly like ``backend.api.security.authentication``: when the request
container/auth config cannot be resolved, the request is denied with 401 —
never allowed. These tests hit real protected HTTP routes (not the
dependency helpers directly) so a regression in the route->dependency wiring
is caught.

Scenarios:
  - unresolved/missing auth config -> 401 (both viewer and admin planes);
  - dev explicit auth-disabled config -> allowed (existing intended dev);
  - valid viewer token -> allowed on viewer routes, 403 on admin routes;
  - valid admin token -> allowed on admin routes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.bootstrap import create_app
from backend.config import AppConfig
from conftest import make_deps as _Deps  # noqa: F401


def _make_app(config: AppConfig):
    deps = _Deps(config=config)
    return create_app(config=config, deps=deps)


def _prod_config(*, viewer: str = "viewer-secret", admin: str = "admin-secret") -> AppConfig:
    return AppConfig(
        render_backend="mock",
        app_env="prod",
        backend_api_token=viewer,
        admin_api_token=admin,
        cors_origins="http://localhost",
    )


def _dev_config() -> AppConfig:
    return AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
    )


def _detach_container(app) -> None:
    """Simulate the request container/auth state being unavailable.

    The app factory wires ``app.state.container``; clearing it models the
    exact scenario ``backend.api.v1.auth._cfg`` used to swallow (``except
    RuntimeError: return None``), which failed open. Fail-closed auth must
    deny (401) here.
    """
    app.state.container = None
    app.state.config = None


# ── unresolved container -> 401 (fail closed) ───────────────────────


def test_viewer_route_401_when_auth_config_unavailable() -> None:
    app = _make_app(_prod_config())
    _detach_container(app)
    with TestClient(app) as client:
        r = client.get("/api/v1/avatars")
    assert r.status_code == 401, r.text


def test_admin_route_401_when_auth_config_unavailable() -> None:
    app = _make_app(_prod_config())
    _detach_container(app)
    with TestClient(app) as client:
        r = client.get("/api/v1/engines")
    assert r.status_code == 401, r.text


# ── dev explicit auth-disabled -> allowed (existing intended behavior) ─


def test_dev_no_tokens_viewer_route_allowed() -> None:
    with TestClient(_make_app(_dev_config())) as client:
        r = client.get("/api/v1/avatars")
    assert r.status_code == 200, r.text


# ── valid credentials -> allowed according to scope ─────────────────


def test_valid_viewer_allowed_on_viewer_route() -> None:
    with TestClient(_make_app(_prod_config())) as client:
        r = client.get("/api/v1/avatars", headers={"Authorization": "Bearer viewer-secret"})
    assert r.status_code == 200, r.text


def test_viewer_token_403_on_admin_route() -> None:
    with TestClient(_make_app(_prod_config())) as client:
        r = client.get("/api/v1/engines", headers={"Authorization": "Bearer viewer-secret"})
    assert r.status_code == 403, r.text


def test_valid_admin_allowed_on_admin_route() -> None:
    with TestClient(_make_app(_prod_config())) as client:
        r = client.get("/api/v1/engines", headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200, r.text
