"""Voice discovery schemas for the TTS service.

Voices reflect the active self-host engine only (Task 1.33: the TTS service
never selects hosted adapters like ElevenLabs or OpenAI Speech).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class VoiceInfo(BaseModel):
    id: str
    name: str = ""
    language: str = "vi"
    engine: str = ""
    description: Optional[str] = None


class VoiceListResponse(BaseModel):
    object: str = "list"
    data: list[VoiceInfo] = Field(default_factory=list)
