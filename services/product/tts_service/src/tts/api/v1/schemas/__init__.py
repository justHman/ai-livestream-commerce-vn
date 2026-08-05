"""Versioned TTS public schemas."""

from tts.api.v1.schemas.speech import (
    SpeechChunk,
    SpeechRequest,
    SpeechResponse,
)
from tts.api.v1.schemas.voices import (
    VoiceInfo,
    VoiceListResponse,
)

__all__ = [
    "SpeechChunk",
    "SpeechRequest",
    "SpeechResponse",
    "VoiceInfo",
    "VoiceListResponse",
]
