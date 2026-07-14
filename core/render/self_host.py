"""Selected self-host avatar renderer placeholder.

AvatarForcing and EchoAvatar stay unavailable until the benchmark selects one
and the avatar ECS service implements the internal AvatarServiceClient protocol.
"""

from __future__ import annotations

from typing import Iterator

from .base import StreamingAvatarBackend, StartOptions, StartResult
from .windows import AudioWindow, VideoWindow


class SelfHostRenderBackend(StreamingAvatarBackend):
    """Fail-loud placeholder for one explicit self-host model target."""

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
