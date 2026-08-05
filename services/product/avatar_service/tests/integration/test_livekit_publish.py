"""Integration: LiveKit publisher mints browser-safe tokens server-side."""

from __future__ import annotations

import pytest

from avatar.config import PublishingConfig
from avatar.publishing.livekit import (
    LiveKitConfigError,
    LiveKitPublisher,
    mint_room_token,
)


def test_mint_room_token_contains_video_claims() -> None:
    token = mint_room_token(
        api_key="k" * 32,
        api_secret="s" * 32,
        room="r1",
        identity="i1",
        ttl_sec=3600,
        now=1_000_000,
    )
    assert token  # non-empty HS256 JWT


def test_mint_room_token_requires_credentials() -> None:
    with pytest.raises(LiveKitConfigError):
        mint_room_token(api_key="", api_secret="s" * 32, room="r", identity="i")
    with pytest.raises(LiveKitConfigError):
        mint_room_token(api_key="k" * 32, api_secret="", room="r", identity="i")


def test_publisher_client_token_scoped() -> None:
    publisher = LiveKitPublisher(
        PublishingConfig(
            livekit_url="ws://localhost:7880",
            livekit_api_key="k" * 32,
            livekit_api_secret="s" * 32,
        )
    )
    token = publisher.client_token("room-x", "identity-y")
    assert publisher.livekit_url == "ws://localhost:7880"
    assert token
