"""Voice routes for the TTS service.

Legacy discovery (``GET /voices``) reflects the active self-host engine only
(Task 1.33) and is preserved untouched. The profile CRUD routes below are the
Change T voice-profile API (tasks 5.2-5.4): enrollment is a raw ``audio/wav``
body plus query fields (no multipart dependency), profiles are tenant-scoped
via ``X-Tenant-Id``, and provider payloads never cross the API boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from tts.api.dependencies import get_engine, get_tenant_id, get_voice_service
from tts.api.security.authorization import require_scope
from tts.api.v1.schemas.common import ErrorResponse
from tts.api.v1.schemas.voices import (
    VoiceInfo,
    VoiceListResponse,
    VoiceProfileCreateRequest,
    VoiceProfileCreateResponse,
    VoiceProfileResponse,
)
from tts.engines.base import TTSEngine
from tts.voices.models import VoiceProfile
from tts.voices.service import VoiceProfileService

router = APIRouter()


def _profile_response(profile: VoiceProfile) -> VoiceProfileResponse:
    return VoiceProfileResponse(
        object="voice_profile",
        voice_profile_id=profile.voice_profile_id,
        tenant_id=profile.tenant_id,
        profile_kind=profile.profile_kind,
        display_name=profile.display_name,
        provider_name=profile.provider_name,
        provider_model_revision=profile.provider_model_revision,
        created_at=profile.created_at,
    )


@router.get("/voices", response_model=VoiceListResponse)
def list_voices(
    _scope: str = Depends(require_scope("tts.voices")),
    engine: TTSEngine = Depends(get_engine),
) -> VoiceListResponse:
    """Return the voices available on the active self-host engine (legacy)."""
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


@router.post(
    "/voices",
    response_model=VoiceProfileCreateResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def create_voice_profile(
    request: Request,
    body: VoiceProfileCreateRequest = Depends(),
    _scope: str = Depends(require_scope("tts.voices")),
    service: VoiceProfileService = Depends(get_voice_service),
    tenant_id: str = Depends(get_tenant_id),
) -> VoiceProfileCreateResponse:
    """Create a voice profile.

    ``preset=true`` seeds a profile for an existing preset display name (no
    reference audio, no provider call). Otherwise the raw request body must be
    a reference WAV; the injected enrollment provider encodes it once and the
    payload is persisted. The provider is wired in cluster 4 — until then the
    cloned path returns 503.
    """
    if body.preset:
        from tts.providers.errors import ProfileNotFoundError
        from tts.voices.presets import PRESET_VOICE_NAMES

        if body.display_name not in PRESET_VOICE_NAMES:
            raise ProfileNotFoundError(f"preset voice {body.display_name!r} not found")
        profiles = service.seed_presets(tenant_id)
        profile = next(p for p in profiles if p.display_name == body.display_name)
        return VoiceProfileCreateResponse(
            object="voice_profile",
            voice_profile_id=profile.voice_profile_id,
            profile_kind=profile.profile_kind,
            display_name=profile.display_name,
        )

    data = await request.body()
    profile = service.enroll_cloned(
        data,
        tenant_id=tenant_id,
        display_name=body.display_name,
        style=body.style,
    )
    return VoiceProfileCreateResponse(
        object="voice_profile",
        voice_profile_id=profile.voice_profile_id,
        profile_kind=profile.profile_kind,
        display_name=profile.display_name,
    )


@router.get(
    "/voices/{voice_profile_id}",
    response_model=VoiceProfileResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def get_voice_profile(
    voice_profile_id: str,
    _scope: str = Depends(require_scope("tts.voices")),
    service: VoiceProfileService = Depends(get_voice_service),
    tenant_id: str = Depends(get_tenant_id),
) -> VoiceProfileResponse:
    """Return provider-neutral metadata for one profile (never the payload)."""
    return _profile_response(service.get_profile(voice_profile_id, tenant_id))


@router.delete(
    "/voices/{voice_profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def delete_voice_profile(
    voice_profile_id: str,
    _scope: str = Depends(require_scope("tts.voices")),
    service: VoiceProfileService = Depends(get_voice_service),
    tenant_id: str = Depends(get_tenant_id),
) -> None:
    """Delete a profile and its payload; 404 when it does not exist here."""
    service.delete_profile(voice_profile_id, tenant_id)
