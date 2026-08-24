"""Remote/provider engine adapters for the shared llm/tts engine seams.

Terraform injects provider-shaped env (``LLM_ADAPTER`` / ``TTS_ADAPTER`` +
base URLs), but the shared ENGINES registries only contained local model
engines — a provider-shaped deployment composed the echo/tone stubs. These
thin adapters wrap the backend-owned outbound clients in the canonical
``LLMEngine``/``TTSEngine`` seam so ``load_engine`` composes the intended
concrete remote client. ``ensure_remote_engines_registered()`` is the single
idempotent registration entrypoint; construction stays lazy inside
``from_config`` and neither import nor registration performs network I/O.
"""

from __future__ import annotations

from typing import Iterator, Optional

import numpy as np

from backend.application.clients.llm.openai_compatible import (
    ChatMessage,
    ChatRequest,
    OpenAICompatibleClient,
)
from backend.application.clients.tts.elevenlabs import ElevenLabsTTSClient
from backend.application.clients.tts.openai_speech import OpenAISpeechTTSClient
from backend.application.clients.tts.self_hosted import SelfHostedTTSClient
from backend.application.contracts.llm_engines import (
    LLMEngine,
    LLMRequest,
    LLMResponse,
    register_engine as register_llm_engine,
)
from backend.application.contracts.tts_engines import (
    AudioChunk,
    TTSEngine,
    TTSRequest,
    register_engine as register_tts_engine,
)


def _chat_request(req: LLMRequest) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role=m["role"], content=m["content"]) for m in req.messages],
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        top_p=req.top_p,
        stop=list(req.stop),
        seed=req.seed,
    )


class OpenAICompatLLMEngine(LLMEngine):
    """LLMEngine seam over the backend-owned OpenAI-compatible client."""

    def __init__(self, client: OpenAICompatibleClient) -> None:
        self._client = client

    @classmethod
    def from_config(cls, cfg: dict) -> "OpenAICompatLLMEngine":
        return cls(
            OpenAICompatibleClient(
                base_url=cfg.get("base_url", ""),
                api_key=cfg.get("api_key", ""),
                model=cfg.get("model", ""),
            )
        )

    def generate(self, req: LLMRequest) -> LLMResponse:
        result = self._client.chat(_chat_request(req))
        return LLMResponse(
            text=result.text,
            finish_reason=result.finish_reason,
            num_prompt_tokens=result.num_prompt_tokens,
            num_generated_tokens=result.num_generated_tokens,
            engine=self.name,
        )

    def stream(self, req: LLMRequest) -> Iterator[str]:
        yield from self._client.chat_stream(_chat_request(req))

    def warmup(self, system_prompt: Optional[str] = None) -> None:
        return None  # no network at startup

    def unload(self) -> None:
        self._client.close()


class RemoteHttpTTSEngine(TTSEngine):
    """TTSEngine seam over the self-host TTS service client."""

    def __init__(self, client: SelfHostedTTSClient, sample_rate: int = 24_000) -> None:
        self._client = client
        self.sample_rate = sample_rate

    @classmethod
    def from_config(cls, cfg: dict) -> "RemoteHttpTTSEngine":
        rate = int(cfg.get("sample_rate", 24_000))
        client = SelfHostedTTSClient(
            base_url=cfg.get("base_url", ""), api_key=cfg.get("api_key", "")
        )
        return cls(client, sample_rate=rate)

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        result = self._client.synthesize(req.text, voice=req.voice or "", language=req.language)
        pcm = np.frombuffer(result.pcm16, dtype="<i2").astype(np.float32) / 32767.0
        return AudioChunk(pcm=pcm, sample_rate=result.sample_rate)

    def warmup(self, text: str = "Xin chào") -> None:
        return None  # no network at startup

    def unload(self) -> None:
        self._client.close()


class ElevenLabsTTSEngine(TTSEngine):
    """TTSEngine seam over the ElevenLabs hosted client."""

    def __init__(self, client: ElevenLabsTTSClient, sample_rate: int = 24_000) -> None:
        self._client = client
        self.sample_rate = sample_rate

    @classmethod
    def from_config(cls, cfg: dict) -> "ElevenLabsTTSEngine":
        rate = int(cfg.get("sample_rate", 24_000))
        kwargs: dict = {}
        if cfg.get("voice_id"):
            kwargs["voice_id"] = cfg["voice_id"]
        if cfg.get("model_id"):
            kwargs["model_id"] = cfg["model_id"]
        client = ElevenLabsTTSClient(api_key=cfg.get("api_key", ""), sample_rate=rate, **kwargs)
        return cls(client, sample_rate=rate)

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        result = self._client.synthesize(req.text)
        return AudioChunk(pcm=result.pcm, sample_rate=result.sample_rate)

    def warmup(self, text: str = "Xin chào") -> None:
        return None  # no network at startup

    def unload(self) -> None:
        self._client.close()


class OpenAISpeechTTSEngine(TTSEngine):
    """TTSEngine seam over the OpenAI-compatible hosted speech client."""

    def __init__(self, client: OpenAISpeechTTSClient, sample_rate: int = 24_000) -> None:
        self._client = client
        self.sample_rate = sample_rate

    @classmethod
    def from_config(cls, cfg: dict) -> "OpenAISpeechTTSEngine":
        rate = int(cfg.get("sample_rate", 24_000))
        kwargs: dict = {}
        if cfg.get("model_id"):
            kwargs["model"] = cfg["model_id"]
        client = OpenAISpeechTTSClient(api_key=cfg.get("api_key", ""), sample_rate=rate, **kwargs)
        return cls(client, sample_rate=rate)

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        result = self._client.synthesize(req.text)
        return AudioChunk(pcm=result.pcm, sample_rate=result.sample_rate)

    def warmup(self, text: str = "Xin chào") -> None:
        return None  # no network at startup

    def unload(self) -> None:
        self._client.close()


def ensure_remote_engines_registered() -> None:
    """Register the provider adapters into the shared llm/tts engine seams.

    Idempotent (the registry is a plain dict assignment), so repeated calls
    are safe. Call once at bootstrap before any ``load_engine`` so a
    provider-shaped deployment composes the remote client instead of the
    echo/tone stub. No network I/O at import or registration.
    """
    register_llm_engine("openai_compat")(OpenAICompatLLMEngine)
    register_tts_engine("remote_http")(RemoteHttpTTSEngine)
    register_tts_engine("elevenlabs")(ElevenLabsTTSEngine)
    register_tts_engine("openai_speech")(OpenAISpeechTTSEngine)
