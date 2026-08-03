"""LiveKit room-token minting and publisher registry for the avatar service.

API/provider credentials stay server-side; only browser-safe LiveKit URL and
client token are ever returned. PCM/video never transits the backend HTTP
plane — it flows directly through LiveKit.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import jwt

from avatar.config import PublishingConfig


class LiveKitConfigError(RuntimeError):
    """Raised when LiveKit credentials are missing or misconfigured."""


def mint_room_token(
    *,
    api_key: str,
    api_secret: str,
    room: str,
    identity: str,
    ttl_sec: int = 3600,
    name: Optional[str] = None,
    can_publish: bool = True,
    can_subscribe: bool = True,
    now: Optional[int] = None,
) -> str:
    """Return a LiveKit HS256 room-join token (browser-safe)."""
    if not api_key or not api_secret:
        raise LiveKitConfigError("LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required")
    if not room or not identity:
        raise ValueError("room and identity must be non-empty")
    timestamp = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "iss": api_key,
        "sub": identity,
        "nbf": timestamp,
        "exp": timestamp + int(ttl_sec),
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": bool(can_publish),
            "canSubscribe": bool(can_subscribe),
        },
    }
    if name:
        payload["name"] = name
    return jwt.encode(payload, api_secret, algorithm="HS256")


class LiveKitPublisher:
    """Owns LiveKit credentials workspace-wide and mints client tokens.

    The publisher registry is a lightweight typed holder for the configured
    LiveKit endpoint. Session coordination (create/interrupt/stop/cleanup)
    lives in avatar/sessions.py; this module only mints browser-safe data.
    """

    def __init__(self, config: PublishingConfig) -> None:
        self._config = config

    @property
    def livekit_url(self) -> str:
        return self._config.livekit_url

    def client_token(self, room: str, identity: str) -> str:
        """Mint a browser-scoped client join token (never the API secret)."""
        return mint_room_token(
            api_key=self._config.livekit_api_key,
            api_secret=self._config.livekit_api_secret,
            room=room,
            identity=identity,
            ttl_sec=self._config.room_ttl_sec,
            can_publish=True,
            can_subscribe=True,
        )

    def close(self) -> None:
        return None