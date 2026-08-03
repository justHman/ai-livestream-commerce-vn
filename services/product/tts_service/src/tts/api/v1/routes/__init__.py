"""Versioned v1 routes for the TTS service."""

from tts.api.v1.routes.speech import router as speech
from tts.api.v1.routes.voices import router as voices

__all__ = ["speech", "voices"]