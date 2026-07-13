"""LiveKit AccessToken mint (minimal HS256 JWT).

Mints room-join tokens for viewers without pulling the full livekit SDK.
Claims follow LiveKit access-token grant shape:
  iss = API key, video.roomJoin + video.room = session room name.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import jwt


class LiveKitConfigError(RuntimeError):
    """Raised when LiveKit credentials/URL are missing."""


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
    """Return a LiveKit AccessToken JWT for joining ``room`` as ``identity``."""
    if not api_key or not api_secret:
        raise LiveKitConfigError("LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required")
    if not room:
        raise ValueError("room must be non-empty")
    if not identity:
        raise ValueError("identity must be non-empty")

    ts = int(now if now is not None else time.time())
    video_grant: dict[str, Any] = {
        "roomJoin": True,
        "room": room,
        "canPublish": bool(can_publish),
        "canSubscribe": bool(can_subscribe),
    }
    payload: dict[str, Any] = {
        "iss": api_key,
        "sub": identity,
        "nbf": ts,
        "exp": ts + int(ttl_sec),
        "video": video_grant,
    }
    if name:
        payload["name"] = name
    return jwt.encode(payload, api_secret, algorithm="HS256")


def mint_session_viewer_token(
    *,
    api_key: str,
    api_secret: str,
    session_id: str,
    ttl_sec: int = 3600,
    now: Optional[int] = None,
) -> str:
    """Mint a subscribe-only token; room name = session_id."""
    return mint_room_token(
        api_key=api_key,
        api_secret=api_secret,
        room=session_id,
        identity=f"viewer-{session_id}",
        ttl_sec=ttl_sec,
        name=f"viewer-{session_id}",
        can_publish=False,
        can_subscribe=True,
        now=now,
    )
