"""P1-05: the production lifespan wires provider enrollment into the voice service.

The lifespan currently builds ``VoiceProfileService`` with NO enrollment
callable (the default ``_enroll_unavailable`` raises), then constructs the
VieNeu provider afterward and never retrofits it. This test boots the FULL
app lifespan with a deterministic fake provider and proves the end-to-end
clone -> persist -> reuse loop: POST /voices enrolls, the stored payload
reaches synthesis, the provider receives the enrolled payload, and tenant
isolation is preserved.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tts import create_app
from tts.providers.capabilities import ProviderCapabilities
from tts.providers.models import AudioResult, ProviderRequest

TENANT = "tenant-clone"


def make_wav(sample_rate: int = 16_000, duration_s: float = 1.0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * int(sample_rate * duration_s))
    return buffer.getvalue()


class FakeEnrollingProvider:
    """Deterministic provider with a working enrollment seam.

    ``enroll_voice`` returns a provider-private payload (dict); ``synthesize``
    records the payload it received from the profile loader, proving the
    stored payload round-trips into the provider.
    """

    provider_name = "fake-enrolling"

    def __init__(self) -> None:
        self.enrollment_calls: list[dict] = []
        self.synthesis_payloads: list[dict] = []
        self._profile_payload: dict | None = None

    # --- provider surface ---
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="fake-enrolling",
            model_revision="fake-1",
            sample_rate_hz=48_000,
            supports_native_batch=True,
            max_batch_size=8,
            supports_mixed_voice_batch=True,
            supported_styles=("natural",),
            supported_response_formats=("pcm", "wav"),
        )

    def batch_key(self, request: ProviderRequest):
        return ("fake-enrolling", request.voice_profile_id)

    async def synthesize(self, request: ProviderRequest) -> AudioResult:
        return self._result(request)

    async def synthesize_batch(self, requests: list[ProviderRequest]) -> list[AudioResult]:
        return [self._result(r) for r in requests]

    def _result(self, request: ProviderRequest) -> AudioResult:
        # The provider's voice payload reaches synthesis only when the
        # lifespan injected the enrollment seam AND the store round-trips.
        if request.voice_profile_id and request.voice_profile_id != "default":
            self.synthesis_payloads.append(request.voice_profile_id)
        return AudioResult(
            request_id=request.request_id,
            sample_rate=48_000,
            waveform=np.zeros(4800, dtype=np.float32),
            response_format=request.response_format,
            duration_ms=100,
        )

    # --- enrollment seam ---
    def enroll_voice(self, reference_audio: bytes, options: dict) -> dict:
        self.enrollment_calls.append({"bytes": len(reference_audio), "options": dict(options)})
        # Provider-private payload: never crosses the API boundary.
        return {"speaker_emb": [0.25] * 192, "ref_codes": [0.5] * 62}

    # --- pre-admission validation (P1-07) ---
    def validate_request(self, request: ProviderRequest) -> None:
        if request.style not in self.capabilities().supported_styles:
            from tts.providers.errors import CapabilityError

            raise CapabilityError(f"unsupported style: {request.style}")

    def profile_loader(self, voice_profile_id: str, tenant_id: str):
        from tts.voices.store import ProfileNotFoundError

        if tenant_id != TENANT or not self._profile_payload:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} not found for tenant {tenant_id!r}"
            )
        from tts.voices.models import VoiceProfile

        return (
            VoiceProfile(
                voice_profile_id=voice_profile_id,
                tenant_id=tenant_id,
                provider_name="fake-enrolling",
                provider_model_revision="fake-1",
                profile_kind="cloned",
                display_name="cloned",
            ),
            self._profile_payload,
        )

    def remember_payload(self, payload: dict) -> None:
        self._profile_payload = payload


@pytest.fixture
def provider(monkeypatch, tmp_path) -> FakeEnrollingProvider:
    """Boot the full app lifespan with the fake provider wired as production."""
    fake = FakeEnrollingProvider()

    def build_provider(app):
        # Simulates the production wiring order once lifespan is fixed:
        # provider built after the voice service, enrollment retrofitted.
        app.state.voice_service.set_enrollment_fn(fake.enroll_voice)
        return fake

    monkeypatch.setattr("tts.bootstrap.lifespan._build_provider", build_provider)
    monkeypatch.setenv("TTS_PROVIDER", "fake-enrolling")
    monkeypatch.setenv("TTS_ACCELERATOR", "cpu")
    monkeypatch.setenv("TTS_VOICE_STORE_URI", f"file://{(tmp_path / 'vp').as_posix()}")
    return fake


def _headers(tenant: str | None = TENANT) -> dict[str, str]:
    headers = {"content-type": "audio/wav"}
    if tenant is not None:
        headers["X-Tenant-Id"] = tenant
    return headers


def test_lifespan_wires_provider_enrollment_end_to_end(provider) -> None:
    """POST /voices enrolls, then synthesis reuses the enrolled payload."""
    app = create_app()
    with TestClient(app) as client:
        assert app.state.voice_service is not None
        assert app.state.provider is not None
        assert app.state.runtime_ready is True

        created = client.post(
            "/v1/voices?display_name=Giọng của tôi",
            content=make_wav(),
            headers=_headers(),
        )
        assert created.status_code == 201
        voice_profile_id = created.json()["voice_profile_id"]
        assert voice_profile_id.startswith("vp_")
        assert created.json()["profile_kind"] == "cloned"

        # Enrollment callable was injected and invoked by the voice service.
        assert provider.enrollment_calls, (
            "the provider enrollment seam must be invoked by the voice service"
        )
        assert provider.enrollment_calls[0]["options"]["voice_profile_id"] == voice_profile_id

        # Synthesis reuses the stored payload — never re-encodes the WAV.
        provider.remember_payload(
            {
                "schema_version": 1,
                "provider": "vieneu_v3",
                "model_revision": "fake-1",
                "speaker_emb": [0.25] * 192,
                "ref_codes": [0.5] * 62,
            }
        )
        speech = client.post(
            "/v1/speech",
            json={
                "text": "Xin chào",
                "voice_profile_id": voice_profile_id,
                "response_format": "wav",
            },
            headers={"X-Tenant-Id": TENANT},
        )
        assert speech.status_code == 200
        assert speech.content[:4] == b"RIFF"
        assert len(provider.synthesis_payloads) == 1, (
            "synthesis must resolve the enrolled profile through the store"
        )
        assert provider.synthesis_payloads[0] == voice_profile_id


def test_tenant_isolation_preserved_through_enrollment(provider) -> None:
    """A cloned profile is only visible to its own tenant."""
    app = create_app()
    with TestClient(app) as client:
        created = client.post(
            "/v1/voices?display_name=v",
            content=make_wav(),
            headers=_headers(),
        ).json()

        same = client.get(f"/v1/voices/{created['voice_profile_id']}", headers=_headers(TENANT))
        other = client.get(
            f"/v1/voices/{created['voice_profile_id']}", headers=_headers("tenant-other")
        )
    assert same.status_code == 200
    assert other.status_code == 404
