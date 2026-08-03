"""LiveAvatar hosted avatar client (backend-owned).

Canonical client location (Task 1.22/1.32). The protocol/serialization is
implemented by the LiveAvatar SDK; this client owns the browser-safe
mapping — only `livekit_url` and browser-scoped `livekit_client_token`
cross the backend boundary. Provider API keys, session tokens, and ws_url
never appear in responses or logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from providers.liveavatar_cloud.sdk.client import (
    LiveAvatarClient as _SDKLiveAvatarClient,
    SANDBOX_AVATAR_ID,
)


class LiveAvatarClientError(RuntimeError):
    """Typed transport failure for the LiveAvatar client."""


@dataclass
class LiveAvatarStartResult:
    """Browser-safe start data returned by the backend (no provider secrets)."""

    session_id: str
    livekit_url: str
    livekit_client_token: str
    mode: str = "LITE"


class LiveAvatarClient:
    """Backend-owned facade over the LiveAvatar SDK with safe DTO mapping."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        base = api_base or os.environ.get("LIVEAVATAR_API_BASE", "") or None
        self._client = _SDKLiveAvatarClient(
            api_key=api_key or os.environ.get("LIVEAVATAR_API_KEY"),
            api_base=base,
            timeout=timeout,
        )

    def start_session(
        self,
        *,
        avatar_id: Optional[str] = None,
        is_sandbox: bool = True,
    ) -> LiveAvatarStartResult:
        """Start a cloud avatar session; return only browser-safe fields.

        The underlying SDK performs the create-session-token + start flow and
        returns `livekit_url` + `livekit_client_token`. Provider session
        tokens and API credentials are never surfaced.
        """
        from providers.liveavatar_cloud.service.conversation import LiteConversation
        from providers.liveavatar_cloud.service.conversation import echo_llm, tone_tts

        convo = LiteConversation(
            client=self._client,
            llm=echo_llm,
            tts=tone_tts,
            avatar_id=avatar_id or SANDBOX_AVATAR_ID,
            is_sandbox=is_sandbox,
        )
        front = convo.start()  # blocking; the API runs this off the event loop
        return LiveAvatarStartResult(
            session_id=str(front["session_id"]),
            livekit_url=str(front["livekit_url"]),
            livekit_client_token=str(front["livekit_client_token"]),
            mode=str(front.get("mode", "LITE")),
        )

    def verify_credentials(self) -> dict:
        """Probe the cheapest authenticated LiveAvatar endpoint."""
        credits = self._client.get_credits()
        return {"credits_available": credits > 0}
