"""VoiceProfileService: enrollment orchestration (Change T task 5.2).

Enrollment deliberately depends on an injected ``enroll_voice_fn`` callable
(provider-bound) rather than a concrete provider — the default raises
``ProviderUnavailableError`` and cluster 4 wires the real VieNeu provider.
Preset seeding (task 5.5) is provider-free and resolves its payload at
synthesis time, so it never needs the callable.

Metrics (task 12.4): enrollment duration + success/failure counters via an
optional ``MetricsRegistry`` — profile ids never become metric labels.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from tts.config import RuntimeConfig
from tts.observability.metrics import MetricsRegistry, record_enrollment
from tts.providers.errors import ProviderUnavailableError
from tts.voices.enrollment import validate_reference_audio
from tts.voices.models import VoiceProfile, new_voice_profile_id
from tts.voices.payloads import encode_vieneu_payload

EnrollFn = Callable[[bytes, dict], dict]


def _enroll_unavailable(data: bytes, options: dict) -> dict:
    raise ProviderUnavailableError(
        "voice enrollment provider is not available yet (wired in cluster 4)"
    )


class VoiceProfileService:
    """Create, read, delete, and seed tenant-scoped voice profiles."""

    def __init__(
        self,
        store: object,
        runtime_config: RuntimeConfig,
        enroll_voice_fn: EnrollFn = _enroll_unavailable,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        self._store = store
        self._config = runtime_config
        self._enroll_voice_fn = enroll_voice_fn
        self._metrics = metrics

    def enroll_cloned(
        self,
        reference_audio: bytes,
        *,
        tenant_id: str,
        display_name: str,
        style: str = "natural",
        denoise: bool = True,
    ) -> VoiceProfile:
        """Validate reference audio, encode via the injected provider, persist."""
        validate_reference_audio(
            reference_audio,
            max_bytes=self._config.voice_max_bytes,
            max_seconds=self._config.voice_max_seconds,
        )
        profile = VoiceProfile(
            voice_profile_id=new_voice_profile_id(),
            tenant_id=tenant_id,
            provider_name=self._config.provider,
            provider_model_revision=self._config.model_revision,
            profile_kind="cloned",
            display_name=display_name,
            provider_payload_location="",  # placeholder; the store keys by id
        )
        options = {
            "display_name": display_name,
            "style": style,
            "denoise": denoise,
            "voice_profile_id": profile.voice_profile_id,
        }
        started = time.monotonic()
        try:
            provider_result = self._enroll_voice_fn(reference_audio, options)
        except Exception:
            record_enrollment(self._metrics, started, succeeded=False)
            raise
        payload = encode_vieneu_payload(
            model_revision=self._config.model_revision,
            speaker_emb=provider_result["speaker_emb"],
            ref_codes=provider_result.get("ref_codes"),
            style=style,
            denoise=denoise,
        )
        self._store.save_profile(profile, payload)
        record_enrollment(self._metrics, started, succeeded=True)
        return profile

    def get_profile(self, voice_profile_id: str, tenant_id: str) -> VoiceProfile:
        profile, _ = self._store.load_profile(voice_profile_id, tenant_id)
        return profile

    def get_profile_payload(
        self, voice_profile_id: str, tenant_id: str
    ) -> tuple[VoiceProfile, dict]:
        """Return (metadata, payload) without re-encoding (task 5.7)."""
        return self._store.load_profile(voice_profile_id, tenant_id)

    def delete_profile(self, voice_profile_id: str, tenant_id: str) -> None:
        self._store.delete_profile(voice_profile_id, tenant_id)

    def list_profiles(self, tenant_id: str) -> list[VoiceProfile]:
        return self._store.list_profiles(tenant_id)

    def seed_presets(self, tenant_id: str) -> list[VoiceProfile]:
        """Idempotently seed preset profiles for the tenant (task 5.5)."""
        from tts.voices.presets import seed_preset_profiles

        return seed_preset_profiles(
            self._store,
            tenant_id,
            provider_name=self._config.provider,
            model_revision=self._config.model_revision,
        )
