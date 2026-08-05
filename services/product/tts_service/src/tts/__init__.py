"""Canonical self-host TTS service package."""

from tts.bootstrap.app_factory import create_app
from tts.engines.base import (
    ENGINES,
    AudioChunk,
    EngineError,
    EngineUnavailable,
    TTSEngine,
    TTSRequest,
    load_engine,
    register_engine,
)

__all__ = [
    "ENGINES",
    "AudioChunk",
    "EngineError",
    "EngineUnavailable",
    "TTSEngine",
    "TTSRequest",
    "create_app",
    "load_engine",
    "register_engine",
]
