"""Avatar engine interfaces — self-host session lifecycle.

Every renderer shares the session lifecycle:
  start(opts)   -> StartResult (frontend-safe creds only)
  stop(sid)     -> tear down the session
  interrupt(sid) -> stop current utterance (barge-in)

Security invariant: start() returns ONLY browser-safe fields
(session_id + livekit_url + livekit_client_token). Provider secrets and
provider session tokens never cross this boundary or appear in responses
or logs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class EngineError(RuntimeError):
    """Typed engine failure surfaced at the API boundary."""


class EngineUnavailable(EngineError):
    """Raised when no engine is started or the engine is not ready."""


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
    mode: str = "self-host"

    def public_dict(self) -> dict[str, Any]:
        """Only the fields safe to hand to the browser."""
        return {
            "session_id": self.session_id,
            "livekit_url": self.livekit_url,
            "livekit_client_token": self.livekit_client_token,
            "mode": self.mode,
        }


class AvatarEngine(ABC):
    """Abstract self-host avatar renderer lifecycle."""

    name: str = "abstract"

    @classmethod
    @abstractmethod
    def from_config(cls, cfg: dict) -> "AvatarEngine":
        """Build from cfg = {engine, model, device, ...}."""
        ...

    @abstractmethod
    def start(self, opts: StartOptions) -> StartResult:
        """Create + start a session, returning browser-safe data."""

    @abstractmethod
    def interrupt(self, session_id: str) -> None:
        """Barge-in: stop the current utterance."""

    @abstractmethod
    def stop(self, session_id: str) -> None:
        """Tear down a session."""

    @abstractmethod
    def session_status(self, session_id: str) -> str:
        """Return a status string for a session."""

    def stop_all(self) -> None:
        """Stop all tracked sessions; stateless backends need no cleanup."""
        return None

    def unload(self) -> None:
        """Free resources when the lifecycle ends."""
        return None
