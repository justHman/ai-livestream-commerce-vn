"""Speech synthesis schemas for the TTS service."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class SpeechRequest(BaseModel):
    """Synthesis request — text, voice, and output bounds."""

    text: str = Field(min_length=1, max_length=4000)
    voice: Optional[str] = None
    language: str = "vi"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: Literal["pcm", "wav"] = "pcm"
    sample_rate: int = Field(default=24_000, ge=8_000, le=48_000)


class SpeechResponse(BaseModel):
    """Metadata for a synthesized utterance."""

    engine: str
    voice: str = ""
    sample_rate: int
    duration_ms: int
    format: str = "pcm"


class SpeechChunk(BaseModel):
    """One streaming audio chunk description (SSE frames carry PCM later)."""

    seq: int
    sample_rate: int
    duration_ms: int
    is_final: bool
