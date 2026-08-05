"""Versioned v1 route registration for the TTS service."""

from __future__ import annotations

from fastapi import APIRouter

from tts.api.v1.routes.speech import router as speech_router
from tts.api.v1.routes.voices import router as voices_router

router = APIRouter()
router.include_router(voices_router)
router.include_router(speech_router)
