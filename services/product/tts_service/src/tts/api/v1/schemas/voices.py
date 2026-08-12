"""Voice schemas for the TTS service.

Legacy discovery (``VoiceInfo``/``VoiceListResponse``) reflects the active
self-host engine only (Task 1.33). The profile CRUD schemas below are the
Change T voice-profile API — provider-neutral metadata, never payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

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


class VoiceProfileCreateRequest(BaseModel):
    """Enrollment input: raw WAV body + these form-adjacent fields.

    The reference WAV travels as the raw request body (``audio/wav``) so the
    enrollment route needs no multipart dependency; these fields ride in the
    query string. ``preset`` requests only seed a profile for a known preset
    name — no reference audio and no provider call.
    """

    display_name: str = Field(min_length=1, max_length=128)
    style: str = "natural"
    preset: bool = False


class VoiceProfileCreateResponse(BaseModel):
    """Opaque profile id plus provider-neutral metadata."""

    object: str = "voice_profile"
    voice_profile_id: str
    profile_kind: Literal["preset", "cloned"]
    display_name: str


class VoiceProfileResponse(BaseModel):
    """Provider-neutral profile metadata (no speaker data ever)."""

    object: str = "voice_profile"
    voice_profile_id: str
    tenant_id: str
    profile_kind: Literal["preset", "cloned"]
    display_name: str
    provider_name: str
    provider_model_revision: str
    created_at: datetime


class VoiceProfileListResponse(BaseModel):
    object: str = "list"
    data: list[VoiceProfileResponse] = Field(default_factory=list)
