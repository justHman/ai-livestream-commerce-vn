"""Canonical self-host TTS service package."""

from .engines.base import (
    AudioChunk,
    TTSEngine,
    TTSRequest,
    ToneEngine,
    load_engine,
    register_engine,
    to_tts_fn,
)

__all__ = [
    "TTSEngine",
    "TTSRequest",
    "AudioChunk",
    "ToneEngine",
    "load_engine",
    "register_engine",
    "to_tts_fn",
]
