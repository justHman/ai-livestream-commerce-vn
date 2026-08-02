"""Conversational LITE loop — wire YOUR LLM + TTS into the avatar.

This is the LITE turn cycle from lite-mode-guide.md, made concrete:

    viewer text --> LLM --> TTS (PCM) --> LiteAudioAgent --> avatar video

The LLM and TTS are injected as plain callables so you can swap in any
backend (SeaLLMs/Qwen via vLLM, viXTTS/Kokoro-VN, etc.) without touching
this file. Defaults are lightweight stubs so the loop runs anywhere.

LLM signature:  fn(user_text: str) -> str
TTS signature:  fn(text: str) -> tuple[bytes, int]   # (pcm16_mono, sample_rate)
                or  fn(text: str) -> Iterable[tuple[bytes, int]]  for streaming
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional, Tuple, Union

from ..sdk.client import LiveAvatarClient, SANDBOX_AVATAR_ID
from .lite_agent import LiteAudioAgent

TtsOut = Union[Tuple[bytes, int], Iterable[Tuple[bytes, int]]]
LLMFn = Callable[[str], str]
TTSFn = Callable[[str], TtsOut]


def echo_llm(user_text: str) -> str:
    """Trivial fallback LLM: echo back. Replace with SeaLLMs/Qwen."""
    return f"Bạn vừa nói: {user_text}. Shop xin phép trả lời nhé!"


def tone_tts(text: str) -> Tuple[bytes, int]:
    """Fallback TTS: a tone whose length scales with text. Replace with viXTTS/Kokoro-VN."""
    from ..sdk import audio

    seconds = max(0.6, min(len(text) / 15.0, 6.0))
    return audio.test_tone(seconds=seconds, freq=330), audio.TARGET_RATE


class LiteConversation:
    """Runs the LITE turn cycle for one session.

    Parameters
    ----------
    client : LiveAvatarClient
    llm : callable
        user_text -> response_text
    tts : callable
        response_text -> (pcm16, rate) or iterable of (pcm16, rate)
    avatar_id : str
        Defaults to the sandbox avatar.
    is_sandbox : bool
    """

    def __init__(
        self,
        client: Optional[LiveAvatarClient] = None,
        llm: LLMFn = echo_llm,
        tts: TTSFn = tone_tts,
        avatar_id: str = SANDBOX_AVATAR_ID,
        is_sandbox: bool = True,
    ) -> None:
        self.client = client or LiveAvatarClient()
        self.llm = llm
        self.tts = tts
        self.avatar_id = avatar_id
        self.is_sandbox = is_sandbox
        self.agent: Optional[LiteAudioAgent] = None
        self._session_token: Optional[str] = None
        self.session_id: Optional[str] = None
        # Frontend-safe; hand these to the browser for video.
        self.livekit_url: Optional[str] = None
        self.livekit_client_token: Optional[str] = None

    def start(self) -> dict:
        """Create + start the LITE session and connect the audio agent.

        Returns the frontend-safe video credentials.
        """
        body = self.client.build_lite_token(self.avatar_id, is_sandbox=self.is_sandbox)
        tok = self.client.create_session_token(body)
        started = self.client.start_session(tok.session_token)
        self._session_token = tok.session_token
        self.session_id = tok.session_id
        self.livekit_url = started.livekit_url
        self.livekit_client_token = started.livekit_client_token

        self.agent = LiteAudioAgent(started.ws_url)
        self.agent.connect(timeout=15.0)
        return {
            "session_id": self.session_id,
            "livekit_url": self.livekit_url,
            "livekit_client_token": self.livekit_client_token,
        }

    def turn(self, user_text: str) -> str:
        """Run one conversation turn: LLM -> TTS -> avatar speaks."""
        if self.agent is None:
            raise RuntimeError("call start() first")

        self.agent.start_listening()
        response = self.llm(user_text)
        self.agent.stop_listening()

        out = self.tts(response)
        # Keep one utterance in flight per session. Returning before
        # agent.speak_ended releases the shared lock while the avatar is still
        # talking, so the next decision cuts the current sentence off.
        if isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], (bytes, bytearray)):
            pcm, rate = out
            self.agent.stream_pcm([pcm], source_rate=rate, wait=True)
        else:
            # iterable of (pcm, rate) — assume constant rate
            chunks = list(out)
            rate = chunks[0][1] if chunks else 24_000
            self.agent.stream_pcm((c[0] for c in chunks), source_rate=rate, wait=True)

        self.agent.start_listening()
        return response

    def speak_verbatim(self, text: str) -> str:
        """Speak `text` directly via TTS — NO LLM. For templated hooks / O(1)
        factual answers from structured catalog data."""
        if self.agent is None:
            raise RuntimeError("call start() first")
        out = self.tts(text)
        if isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], (bytes, bytearray)):
            pcm, rate = out
            self.agent.stream_pcm([pcm], source_rate=rate, wait=True)
        else:
            chunks = list(out)
            rate = chunks[0][1] if chunks else 24_000
            self.agent.stream_pcm((c[0] for c in chunks), source_rate=rate, wait=True)
        return text

    def stop(self) -> None:
        if self.agent is not None:
            self.agent.close()
            self.agent = None
        if self._session_token is not None:
            self.client.stop_session(self._session_token)
            self._session_token = None
