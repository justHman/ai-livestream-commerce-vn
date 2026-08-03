"""Voice discovery route for the TTS service.

Voices reflect the active self-host engine only (Task 1.33). No hosted
adapter voices (ElevenLabs/OpenAI Speech) appear here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from tts.api.dependencies import get_engine
from tts.api.security.authorization import require_scope
from tts.api.v1.schemas.voices import VoiceInfo, VoiceListResponse
from tts.engines.base import TTSEngine

router = APIRouter()


@router.get("/voices", response_model=VoiceListResponse)
def list_voices(
    _scope: str = Depends(require_scope("tts.voices")),
    engine: TTSEngine = Depends(get_engine),
) -> VoiceListResponse:
    """Return the voices available on the active self-host engine."""
    return VoiceListResponse(
        data=[
            VoiceInfo(
                id="default",
                name="Default voice",
                language="vi",
                engine=engine.name,
                description=f"Active engine: {engine.name}",
            )
        ]
    )
