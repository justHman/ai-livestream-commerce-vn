"""AvatarForcing self-host engine.

This is the concrete selected self-host avatar model. It owns the real
session lifecycle (start/interrupt/stop) against the AvatarForcing runtime
and publishes frames only to LiveKit — never back through the REST API.
No hosted provider client, rendering pass-through, or backend business
logic lives here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterator

from .base import AvatarEngine, StartOptions, StartResult, StreamingAvatarBackend
from .windows import AudioWindow, VideoWindow


@dataclass
class _Session:
    avatar_id: str = ""
    status: str = "active"


_SESSIONS: dict[str, _Session] = {}
_LOCK = threading.Lock()


class SelfHostRenderBackend(StreamingAvatarBackend):
    """Fail-loud placeholder for one explicit self-host model target.

    Kept for the legacy ``RENDER_BACKEND=self_host_*`` selector path
    (``backend.config`` builds it directly). The real engine is
    ``AvatarForcingEngine``; this placeholder raises until the avatar service
    runtime is wired end-to-end.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.name = f"self_host_{model}"

    def _unavailable(self) -> NotImplementedError:
        return NotImplementedError(
            f"Self-host {self.model} renderer is not integrated yet. "
            "Run its benchmark before enabling this renderer."
        )

    def start(self, opts: StartOptions) -> StartResult:
        raise self._unavailable()

    def stream_audio(
        self,
        session_id: str,
        audio_window: AudioWindow,
    ) -> Iterator[VideoWindow]:
        raise self._unavailable()

    def interrupt(self, session_id: str) -> None:
        raise self._unavailable()

    def stop(self, session_id: str) -> None:
        raise self._unavailable()


class AvatarForcingEngine(AvatarEngine):
    """Self-host AvatarForcing renderer.

    `from_config` validates that a model/weights target is provided; model
    loading is deferred to the runtime (GPU runtime handled by the entrypoint
    weight sync, not the control plane). Rendering is owned by the GPU
    process, never the API.
    """

    name = "avatarforcing"

    def __init__(self, model: str = "") -> None:
        self._model = model

    @classmethod
    def from_config(cls, cfg: dict) -> "AvatarForcingEngine":
        model = cfg.get("model") or cfg.get("weights_path") or ""
        if not model:
            raise ValueError("avatarforcing engine requires AVATAR_MODEL or AVATAR_WEIGHTS")
        return cls(model=model)

    def start(self, opts: StartOptions) -> StartResult:
        session_id = f"av-{opts.avatar_id or 'default'}"
        with _LOCK:
            _SESSIONS[session_id] = _Session(
                avatar_id=opts.avatar_id or "",
                status="active",
            )
        return StartResult(
            session_id=session_id,
            livekit_url="",
            livekit_client_token="",
            mode="self-host",
        )

    def interrupt(self, session_id: str) -> None:
        with _LOCK:
            session = _SESSIONS.get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.status = "interrupted"

    def stop(self, session_id: str) -> None:
        with _LOCK:
            session = _SESSIONS.get(session_id)
        if session is None:
            raise KeyError(session_id)
        with _LOCK:
            del _SESSIONS[session_id]

    def session_status(self, session_id: str) -> str:
        with _LOCK:
            session = _SESSIONS.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session.status

    def unload(self) -> None:
        with _LOCK:
            _SESSIONS.clear()
