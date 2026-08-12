"""Shared factories for voice profile tests."""

from __future__ import annotations

from tts.voices.models import VoiceProfile


def make_profile(
    voice_profile_id: str,
    tenant_id: str,
    *,
    display_name: str = "Default name",
    kind: str = "preset",
) -> VoiceProfile:
    return VoiceProfile(
        voice_profile_id=voice_profile_id,
        tenant_id=tenant_id,
        provider_name="vieneu_v3",
        provider_model_revision="pnnbao-ump/VieNeu-TTS-v3-Turbo",
        profile_kind=kind,
        display_name=display_name,
        provider_payload_location=f"preset://{display_name}",
    )
