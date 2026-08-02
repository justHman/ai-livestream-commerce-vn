"""Render backend interfaces — the seam that makes avatar renderers swappable.

All renderers share the session lifecycle:

  start(opts)       -> StartResult (frontend-safe creds only)
  interrupt(sid)   -> stop current utterance (barge-in)
  stop(sid)        -> tear down the session

Then they split by how they produce speech/video:

  FullPipelineBackend.say(sid, text, generate=True) -> str
      The backend owns the full LLM/TTS/render turn. LiveAvatar cloud uses this.

  StreamingAvatarBackend.stream_audio(sid, audio_window) -> Iterator[VideoWindow]
      Core owns LLM+TTS and streams AudioWindow chunks into the renderer. Mock and
      future self-host avatar models use this.

Security invariant: start() returns ONLY frontend-safe fields (session_id +
livekit_url + livekit_client_token). Secrets (X-API-KEY, session_token, audio
ws_url) never cross this boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .windows import AudioWindow, VideoWindow


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
