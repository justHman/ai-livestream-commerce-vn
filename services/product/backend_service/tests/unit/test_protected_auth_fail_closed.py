"""Fail-closed protected auth + production auth-token validation (Cluster B task B.5).

R8.9: ``require_viewer`` / ``require_admin`` returned (allowed) when the
container/config lookup failed, leaving protected routes open. They must
deny (401) when auth configuration cannot be resolved — health endpoints
stay public because they never use these dependencies.

R8.1/R8.2: real production must reject empty/placeholder
``backend_api_token`` / ``admin_api_token`` before the service becomes
ready. The helper is tested directly (not through ``create_app``, which
would also trip the DATABASE_URL production guard).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.security.authentication import require_admin, require_viewer
from backend.bootstrap.app_factory import _validate_production_auth_tokens
from backend.config import AppConfig


def _fake_request() -> SimpleNamespace:
    """A minimal request whose app.state.container resolves to None."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=None)))


async def test_require_viewer_fails_closed_when_no_config() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_viewer(_fake_request())
    assert exc_info.value.status_code == 401


async def test_require_admin_fails_closed_when_no_config() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(_fake_request())
    assert exc_info.value.status_code == 401


def test_validate_production_auth_tokens_rejects_placeholder_viewer() -> None:
    config = AppConfig(
        app_env="prod",
        backend_api_token="CHANGE_ME",
        admin_api_token="real-admin",
    )

    with pytest.raises(RuntimeError, match="backend_api_token"):
        _validate_production_auth_tokens(config)


def test_validate_production_auth_tokens_rejects_empty_admin() -> None:
    config = AppConfig(
        app_env="prod",
        backend_api_token="real-viewer",
        admin_api_token="",
    )

    with pytest.raises(RuntimeError, match="admin_api_token"):
        _validate_production_auth_tokens(config)


def test_validate_production_auth_tokens_accepts_real_tokens() -> None:
    config = AppConfig(
        app_env="prod",
        backend_api_token="real-viewer",
        admin_api_token="real-admin",
    )

    _validate_production_auth_tokens(config)


def test_validate_production_auth_tokens_noop_in_dev() -> None:
    config = AppConfig(app_env="dev", backend_api_token="", admin_api_token="")

    _validate_production_auth_tokens(config)
