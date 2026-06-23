"""RenderBackend — the seam that makes the avatar renderer swappable.

Every renderer (LiveAvatar cloud now, self-host diffusion later) implements
this interface. The API layer (core/api/v1.py) only ever talks to this ABC,
so adding/swapping a renderer needs ZERO changes above this line.

Contract (all session-scoped):
  start(opts)            -> StartResult (frontend-safe creds only)
  say(session_id, text)  -> reply text (LLM->TTS->stream happens inside)
  interrupt(session_id)  -> stop current utterance (barge-in)
  stop(session_id)       -> tear down the session

Security invariant: start() returns ONLY frontend-safe fields
(session_id + livekit_url + livekit_client_token). Secrets (X-API-KEY,
session_token, audio ws_url) never cross this boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class StartOptions:
    """Options for starting a render session."""

    avatar_id: Optional[str] = None
    is_sandbox: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StartResult:
    """Frontend-safe result of starting a session."""

    session_id: str
    livekit_url: str
    livekit_client_token: str
    mode: str = "LITE"

    def public_dict(self) -> dict[str, Any]:
        """Only the fields safe to hand to the browser."""
        return {
            "session_id": self.session_id,
            "livekit_url": self.livekit_url,
            "livekit_client_token": self.livekit_client_token,
            "mode": self.mode,
        }


class RenderBackend(ABC):
    """Abstract avatar renderer."""

    name: str = "abstract"

    @abstractmethod
    def start(self, opts: StartOptions) -> StartResult:
        """Create + start a session. Blocking; the API runs it off-loop."""
        ...

    @abstractmethod
    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        """One turn. generate=True: LLM(text)->TTS->stream (text is the user msg
        or an LLM prompt). generate=False: speak `text` VERBATIM via TTS only
        (no LLM) — used for templated hooks / O(1) factual answers. Returns the
        spoken text."""
        ...

    @abstractmethod
    def interrupt(self, session_id: str) -> None:
        """Barge-in: stop the current utterance."""
        ...

    @abstractmethod
    def stop(self, session_id: str) -> None:
        """Tear down a session."""
        ...
