"""Avatar engine interfaces — render backend seam + self-host lifecycle.

Two coexisting interface families:

* ``RenderBackend`` / ``FullPipelineBackend`` / ``StreamingAvatarBackend`` —
  the legacy renderer seam. ``FullPipelineBackend.say()`` owns the full
  LLM/TTS/render turn (LiveAvatar cloud); ``StreamingAvatarBackend.stream_audio()``
  consumes TTS ``AudioWindow`` chunks and yields ``VideoWindow`` (mock, remote,
  future self-host). ``backend.application.render.engines_base`` re-exports these.

* ``AvatarEngine`` — the self-host engine lifecycle (start/interrupt/stop/
  session_status/stop_all/unload) with ``from_config`` construction.
  ``AvatarForcingEngine`` implements it.

Security invariant: start() returns ONLY browser-safe fields
(session_id + livekit_url + livekit_client_token). Provider secrets and
provider session tokens never cross this boundary or appear in responses
or logs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .windows import AudioWindow, VideoWindow


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
    """Abstract renderer lifecycle shared by all backend types."""

    name: str = "abstract"

    @abstractmethod
    def start(self, opts: StartOptions) -> StartResult:
        """Create + start a session. Blocking; the API runs it off-loop."""
        ...

    @abstractmethod
    def interrupt(self, session_id: str) -> None:
        """Barge-in: stop the current utterance."""
        ...

    @abstractmethod
    def stop(self, session_id: str) -> None:
        """Tear down a session."""
        ...

    def stop_all(self) -> None:
        """Stop all tracked sessions; stateless backends need no cleanup."""
        return None

    def session_status(self, session_id: str) -> str:
        """Return a status string for a session.

        Concrete default: returns ``"unknown"`` so backends that do not track
        per-session status (e.g. the cloud backend) are unaffected. Streaming
        backends that track session state (e.g. MockRenderBackend) override this
        to return the real status and raise KeyError for unknown sessions.
        """
        return "unknown"


class FullPipelineBackend(RenderBackend):
    """Renderer that owns a full text -> speech/video turn internally."""

    @abstractmethod
    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        """One turn.

        generate=True: backend does LLM(text)->TTS->stream (text is the user msg
        or an LLM prompt). generate=False: speak ``text`` VERBATIM via TTS only
        (no LLM) — used for templated hooks / O(1) factual answers. Returns the
        spoken text.
        """
        ...


class StreamingAvatarBackend(RenderBackend):
    """Renderer that consumes TTS AudioWindows and yields VideoWindows."""

    @abstractmethod
    def stream_audio(
        self,
        session_id: str,
        audio_window: AudioWindow,
    ) -> Iterator[VideoWindow]:
        """Render one TTS audio window into one or more video windows."""
        ...


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
