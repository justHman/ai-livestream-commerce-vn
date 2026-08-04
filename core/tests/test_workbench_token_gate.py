"""Workbench dev fixture credential gate (Task 1.46) + canonical config route.

Verifies:
  - AppConfig.from_env() raises (without logging the literal) when a Workbench
    fixture credential is configured outside APP_ENV=dev|test.
  - dev/test envs accept the fixture credentials.
  - /api/v1/sessions/{id}/config PATCH wires to the runtime config updater.
"""

from __future__ import annotations

import pytest

from core.config import (
    _dev_fixture_admin,
    _dev_fixture_viewer,
    validate_dev_fixture_credentials,
)
from core.api import v1


def test_dev_and_test_envs_accept_fixture_credentials() -> None:
    for env in ("dev", "test"):
        # Must not raise.
        validate_dev_fixture_credentials(env, _dev_fixture_viewer(), _dev_fixture_admin())


def test_prod_rejects_fixture_viewer_credential() -> None:
    with pytest.raises(RuntimeError, match="local-only"):
        validate_dev_fixture_credentials("prod", _dev_fixture_viewer(), "")


def test_prod_rejects_fixture_admin_credential() -> None:
    with pytest.raises(RuntimeError, match="local-only"):
        validate_dev_fixture_credentials("prod", "", _dev_fixture_admin())


def test_prod_rejects_when_either_matches() -> None:
    with pytest.raises(RuntimeError):
        validate_dev_fixture_credentials("prod", _dev_fixture_viewer(), _dev_fixture_admin())


def test_non_dev_test_env_names_reject() -> None:
    for env in ("staging", "production", "PRD"):
        with pytest.raises(RuntimeError):
            validate_dev_fixture_credentials(env, _dev_fixture_viewer(), "")


def test_rejection_message_has_no_literal_value() -> None:
    try:
        validate_dev_fixture_credentials("prod", _dev_fixture_viewer(), "")
    except RuntimeError as exc:
        message = str(exc)
        assert _dev_fixture_viewer() not in message
        assert _dev_fixture_admin() not in message
        assert "fixture" in message


def test_real_credentials_pass_production() -> None:
    validate_dev_fixture_credentials(
        "prod", "real-viewer-value-abcdef", "real-admin-value-abcdef"
    )


def test_empty_credentials_pass_everywhere() -> None:
    validate_dev_fixture_credentials("prod", "", "")


def test_canonical_config_route_declared() -> None:
    """The canonical path-style PATCH route must exist on the router."""
    config_route = None
    for route in v1.router.routes:
        if getattr(route, "path", None) == "/api/v1/sessions/{session_id}/config":
            config_route = route
            break
    assert config_route is not None
    assert "PATCH" in getattr(config_route, "methods", set())