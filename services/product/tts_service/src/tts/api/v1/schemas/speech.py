"""Speech synthesis schemas for the TTS service."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

# Change T: provider-neutral scheduling/tracing fields. All optional with safe
# defaults so existing callers (backend self_hosted.py) are unaffected; the
# scheduler consumes them in the cluster that wires the runtime. Provider-
# specific payloads (speaker embeddings, reference codes) intentionally have
# no field here — unknown fields are rejected by pydantic.
RequestId = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class SpeechRequest(BaseModel):
    """Synthesis request — text, voice, and output bounds.

    `model_config` forbids unknown fields so provider-specific payloads
    (speaker embeddings, reference codes, tensors) fail loudly at the API
    boundary instead of being silently dropped or leaking into the runtime.
    """

    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=4000)
    voice: Optional[str] = None
    language: str = "vi"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: Literal["pcm", "wav"] = "pcm"
    sample_rate: int = Field(default=24_000, ge=8_000, le=48_000)
    # Change T fields (accepted now, consumed by the scheduler later).
    session_id: Optional[RequestId] = None
    utterance_id: Optional[RequestId] = None
    chunk_seq: int = Field(default=0, ge=0)
    voice_profile_id: Optional[str] = Field(default=None, max_length=256)
    style: str = "natural"
    priority: Literal["normal", "high"] = "normal"


class SpeechResponse(BaseModel):
    """Metadata for a synthesized utterance."""

    engine: str
    voice: str = ""
    sample_rate: int
    duration_ms: int
    format: str = "pcm"


class CapabilityResponse(BaseModel):
    """Provider-neutral capability facts.

    Shape mirrors `ProviderCapabilities`; provider payloads (speaker
    embeddings, reference codes, tensors) never serialize here.
    """

    provider_name: str
    model_revision: str
    sample_rate_hz: int
    supports_native_batch: bool = False
    max_batch_size: int = 1
    supports_voice_cloning: bool = False
    supports_mixed_voice_batch: bool = False
    supported_styles: list[str] = Field(default_factory=lambda: ["natural"])
    supported_expressive_cues: list[str] = Field(default_factory=list)
    supported_response_formats: list[str] = Field(default_factory=lambda: ["pcm", "wav"])


class SpeechChunk(BaseModel):
    """One streaming audio chunk description (SSE frames carry PCM later)."""

    seq: int
    sample_rate: int
    duration_ms: int
    is_final: bool
