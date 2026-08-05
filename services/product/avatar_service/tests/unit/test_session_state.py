"""Session manager state and secret isolation."""

from __future__ import annotations

import pytest

from avatar.config import PublishingConfig
from avatar.engines.avatarforcing import AvatarForcingEngine
from avatar.engines.base import StartOptions
from avatar.publishing.livekit import LiveKitPublisher
from avatar.sessions import SessionManager


def _manager() -> SessionManager:
    engine = AvatarForcingEngine.from_config({"model": "m"})
    publisher = LiveKitPublisher(
        PublishingConfig(
            livekit_url="ws://localhost:7880",
            livekit_api_key="k" * 32,
            livekit_api_secret="s" * 32,
        )
    )
    return SessionManager(engine, publisher)


def test_create_returns_browser_safe_only() -> None:
    manager = _manager()
    result = manager.create(StartOptions(avatar_id="a1"))
    # No API key / secret / provider token in the public DTO.
    assert "livekit_api_key" not in result.public_dict()
    assert "livekit_api_secret" not in result.public_dict()
    assert "api_key" not in result.public_dict()
    assert result.livekit_url == "ws://localhost:7880"
    assert result.livekit_client_token


def test_interrupt_updates_status() -> None:
    manager = _manager()
    result = manager.create(StartOptions(avatar_id="a1"))
    manager.interrupt(result.session_id)
    assert manager.status(result.session_id) == "interrupted"


def test_stop_removes_session() -> None:
    manager = _manager()
    result = manager.create(StartOptions(avatar_id="a1"))
    manager.stop(result.session_id)
    # SessionManager drops its record; the engine also drops the session.
    with pytest.raises(KeyError):
        manager.status(result.session_id)


def test_cleanup_stops_all() -> None:
    manager = _manager()
    manager.create(StartOptions(avatar_id="a1"))
    manager.create(StartOptions(avatar_id="a2"))
    manager.cleanup()  # must not raise
