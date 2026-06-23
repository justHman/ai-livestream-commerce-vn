"""CloudRenderBackend — LiveAvatar cloud, behind the RenderBackend seam.

Reuses the existing, sandbox-verified `liveavatar_api` code (LiveAvatarClient,
LiteConversation, LiteAudioAgent) without rewriting it. This adapter is the ONLY
place core/ depends on liveavatar_api; swapping to self-host changes nothing above.

LLM/TTS are injected via configure() — Colab passes local model callables, AWS
points at a shared vLLM/TTS endpoint. Defaults are the lightweight stubs so the
backend runs anywhere out of the box.
"""

from __future__ import annotations

from typing import Optional

from liveavatar_api.backend.client import LiveAvatarClient, SANDBOX_AVATAR_ID
from liveavatar_api.backend.conversation import (
    LiteConversation,
    echo_llm,
    tone_tts,
)

from .base import RenderBackend, StartOptions, StartResult

# Module-level injected backends (overridden by the Colab/cloud launcher).
_LLM_FN = echo_llm
_TTS_FN = tone_tts


def configure(llm_fn=None, tts_fn=None) -> None:
    """Inject real LLM/TTS callables before serving."""
    global _LLM_FN, _TTS_FN
    if llm_fn is not None:
        _LLM_FN = llm_fn
    if tts_fn is not None:
        _TTS_FN = tts_fn


class CloudRenderBackend(RenderBackend):
    """Renders via LiveAvatar cloud (LITE mode)."""

    name = "cloud"

    def __init__(self, client: Optional[LiveAvatarClient] = None) -> None:
        self._client = client or LiveAvatarClient()
        self._convos: dict[str, LiteConversation] = {}

    def start(self, opts: StartOptions) -> StartResult:
        convo = LiteConversation(
            client=self._client,
            llm=_LLM_FN,
            tts=_TTS_FN,
            avatar_id=opts.avatar_id or SANDBOX_AVATAR_ID,
            is_sandbox=opts.is_sandbox,
        )
        front = convo.start()  # blocking; API runs this off the event loop
        self._convos[front["session_id"]] = convo
        return StartResult(
            session_id=front["session_id"],
            livekit_url=front["livekit_url"],
            livekit_client_token=front["livekit_client_token"],
            mode="LITE",
        )

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        convo = self._convos.get(session_id)
        if convo is None:
            raise KeyError(session_id)
        if generate:
            return convo.turn(text)
        return convo.speak_verbatim(text)

    def interrupt(self, session_id: str) -> None:
        convo = self._convos.get(session_id)
        if convo is not None and convo.agent is not None:
            convo.agent.interrupt()

    def stop(self, session_id: str) -> None:
        convo = self._convos.pop(session_id, None)
        if convo is None:
            raise KeyError(session_id)
        convo.stop()

    def has(self, session_id: str) -> bool:
        return session_id in self._convos
