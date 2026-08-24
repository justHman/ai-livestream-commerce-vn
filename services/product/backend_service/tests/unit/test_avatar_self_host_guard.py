"""Fail-loud self-host Avatar guard (R0.3 / Decision 5).

Selecting ``AVATAR_ADAPTER=self_hosted`` must fail clearly at startup: the
avatar_service only has a test stub (``AvatarForcingEngine(model="mock")``)
that must never advertise production-ready self-host. Production rejects it
unconditionally; dev/test requires the explicit ``ALLOW_STUB_AVATAR_TEST_ONLY``
escape. Mirrors the B.1 production local-engine guard placement — real
composition path only (no injected deps/container).
"""

from __future__ import annotations

import pytest

from backend.bootstrap.app_factory import create_app
from backend.config import AppConfig


def test_production_rejects_selfhost_avatar() -> None:
    config = AppConfig(
        app_env="prod",
        cors_origins="https://shop.example",
        avatar_adapter="self_hosted",
    )

    with pytest.raises(RuntimeError, match="self-host Avatar"):
        create_app(config=config)


def test_dev_selfhost_avatar_rejected_without_test_flag() -> None:
    config = AppConfig(app_env="dev", avatar_adapter="self_hosted")

    with pytest.raises(RuntimeError, match="test mode"):
        create_app(config=config)


def test_dev_selfhost_avatar_allowed_with_test_flag() -> None:
    config = AppConfig(
        app_env="dev",
        avatar_adapter="self_hosted",
        allow_stub_avatar_test_only=True,
    )

    # The avatar guard must not fire; any other RuntimeError (e.g. from the
    # composition root booting) should surface as a failure, not be swallowed.
    try:
        app = create_app(config=config)
    except RuntimeError as exc:
        if "self-host Avatar" in str(exc):
            pytest.fail(f"avatar guard fired despite ALLOW_STUB_AVATAR_TEST_ONLY=1: {exc}")
        raise
    assert app is not None
