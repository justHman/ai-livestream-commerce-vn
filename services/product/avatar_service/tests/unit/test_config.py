"""Config validation for the avatar self-host service."""

from __future__ import annotations

import pytest

from avatar.config import (
    EngineConfig,
    PublishingConfig,
    SecurityConfig,
)


def test_self_host_engine_accepted() -> None:
    ec = EngineConfig(engine="avatarforcing", model="m")
    assert ec.engine == "avatarforcing"


def test_remote_avatar_rejected_as_engine() -> None:
    with pytest.raises(ValueError, match="not a valid self-host engine"):
        EngineConfig(engine="remote_avatar")


def test_liveavatar_rejected_as_engine() -> None:
    with pytest.raises(ValueError, match="not a valid self-host engine"):
        EngineConfig(engine="liveavatar")


def test_baidu_xiling_rejected_as_engine() -> None:
    with pytest.raises(ValueError, match="not a valid self-host engine"):
        EngineConfig(engine="baidu_xiling")


def test_publishing_config_requires_credentials() -> None:
    with pytest.raises(ValueError, match="LIVEKIT_URL"):
        PublishingConfig(livekit_url="")
    with pytest.raises(ValueError, match="API_KEY"):
        PublishingConfig(livekit_url="ws://x", livekit_api_key="", livekit_api_secret="s")
    with pytest.raises(ValueError, match="API_SECRET"):
        PublishingConfig(livekit_url="ws://x", livekit_api_key="k", livekit_api_secret="")


def test_security_config_requires_token() -> None:
    with pytest.raises(ValueError, match="required when"):
        SecurityConfig(auth_enabled=True)


def test_engine_config_to_cfg_dict() -> None:
    ec = EngineConfig(engine="avatarforcing", model="m")
    d = ec.to_cfg_dict()
    assert d["engine"] == "avatarforcing"
    assert d["model"] == "m"
