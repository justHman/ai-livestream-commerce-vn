"""LiveKit room-token minting owned by the avatar publishing runtime."""

from __future__ import annotations

import time
from typing import Any, Optional

import jwt


class LiveKitConfigError(RuntimeError):
    """Raised when LiveKit credentials are missing."""


def mint_room_token(
    *,
    api_key: str,
    api_secret: str,
    room: str,
    identity: str,
    ttl_sec: int = 3600,
    name: Optional[str] = None,
    can_publish: bool = False,
    can_subscribe: bool = True,
    now: Optional[int] = None,
) -> str:
    """Return a LiveKit HS256 room-join token."""
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
