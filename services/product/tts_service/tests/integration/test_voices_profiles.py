"""Integration: voice profile enrollment API (5.2/5.3/5.4)."""

from __future__ import annotations

import io
import wave

from fastapi.testclient import TestClient

from tts import create_app
from tts.api.dependencies import get_voice_service
from tts.config import RuntimeConfig, SecurityConfig
from tts.voices.service import VoiceProfileService
from tts.voices.store import FilesystemVoiceProfileStore


def make_wav(sample_rate: int = 16_000, duration_s: float = 1.0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * int(sample_rate * duration_s))
    return buffer.getvalue()


class FakeEnroller:
    def __call__(self, data: bytes, options: dict) -> dict:
        return {"speaker_emb": [0.1] * 192, "ref_codes": [0.2] * 62}


def _service(tmp_path, *, enroller=FakeEnroller()) -> VoiceProfileService:
    return VoiceProfileService(
        FilesystemVoiceProfileStore(tmp_path / "vp"),
        RuntimeConfig(),
        enroller,
    )


def _app(tmp_path) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_voice_service] = lambda: _service(tmp_path)
    return TestClient(app)


def test_post_enrolls_cloned_voice(tmp_path) -> None:
    with _app(tmp_path) as client:
        resp = client.post(
            "/v1/voices?display_name=Giọng của tôi",
            content=make_wav(),
            headers={"content-type": "audio/wav"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["voice_profile_id"].startswith("vp_")
    assert body["profile_kind"] == "cloned"
    assert body["display_name"] == "Giọng của tôi"


def test_get_returns_provider_neutral_metadata(tmp_path) -> None:
    with _app(tmp_path) as client:
        created = client.post(
            "/v1/voices?display_name=v",
            content=make_wav(),
            headers={"content-type": "audio/wav"},
        ).json()
        resp = client.get(f"/v1/voices/{created['voice_profile_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "default"
    assert body["profile_kind"] == "cloned"
    assert body["provider_name"] == "vieneu_v3"
    assert body["provider_model_revision"]
    assert "speaker_emb" not in body
    assert "ref_codes" not in body
    assert "payload" not in body


def test_get_after_restart_new_service(tmp_path) -> None:
    """Same filesystem dir + fresh service still resolves the profile (5.6)."""
    store_dir = tmp_path / "vp"
    first = create_app()
    first.dependency_overrides[get_voice_service] = lambda: VoiceProfileService(
        FilesystemVoiceProfileStore(store_dir), RuntimeConfig(), FakeEnroller()
    )
    with TestClient(first) as client:
        created = client.post(
            "/v1/voices?display_name=v",
            content=make_wav(),
            headers={"content-type": "audio/wav"},
        ).json()

    second = create_app()
    second.dependency_overrides[get_voice_service] = lambda: VoiceProfileService(
        FilesystemVoiceProfileStore(store_dir), RuntimeConfig()
    )
    with TestClient(second) as client:
        resp = client.get(f"/v1/voices/{created['voice_profile_id']}")
    assert resp.status_code == 200


def test_delete_removes_profile(tmp_path) -> None:
    with _app(tmp_path) as client:
        created = client.post(
            "/v1/voices?display_name=v",
            content=make_wav(),
            headers={"content-type": "audio/wav"},
        ).json()
        deleted = client.delete(f"/v1/voices/{created['voice_profile_id']}")
        assert deleted.status_code == 204
        assert client.get(f"/v1/voices/{created['voice_profile_id']}").status_code == 404


def test_delete_missing_profile_404(tmp_path) -> None:
    with _app(tmp_path) as client:
        assert client.delete("/v1/voices/vp-does-not-exist").status_code == 404


def test_tenant_isolation_across_headers(tmp_path) -> None:
    """X-Tenant-Id scopes profile visibility; cross-tenant reads are 404."""
    with _app(tmp_path) as client:
        created = client.post(
            "/v1/voices?display_name=Giọng của tôi",
            content=make_wav(),
            headers={"content-type": "audio/wav", "X-Tenant-Id": "tenant-a"},
        ).json()
        same_tenant = client.get(
            f"/v1/voices/{created['voice_profile_id']}",
            headers={"X-Tenant-Id": "tenant-a"},
        )
        other_tenant = client.get(
            f"/v1/voices/{created['voice_profile_id']}",
            headers={"X-Tenant-Id": "tenant-b"},
        )
        no_header = client.get(f"/v1/voices/{created['voice_profile_id']}")
    assert same_tenant.status_code == 200
    assert other_tenant.status_code == 404  # existence not leaked
    assert no_header.status_code == 404  # default tenant != tenant-a


def test_preset_seed_endpoint(tmp_path) -> None:
    with _app(tmp_path) as client:
        resp = client.post("/v1/voices?display_name=Phạm Tuyên&preset=true")
        assert resp.status_code == 201
        body = resp.json()
        assert body["profile_kind"] == "preset"
        get = client.get(f"/v1/voices/{body['voice_profile_id']}")
    assert get.status_code == 200


def test_preset_seed_unknown_name_404(tmp_path) -> None:
    with _app(tmp_path) as client:
        resp = client.post("/v1/voices?display_name=Không tồn tại&preset=true")
    assert resp.status_code == 404


def test_malformed_wav_422(tmp_path) -> None:
    with _app(tmp_path) as client:
        resp = client.post(
            "/v1/voices?display_name=v",
            content=b"not a wav",
            headers={"content-type": "audio/wav"},
        )
    assert resp.status_code == 422


def test_cloned_enrollment_503_without_provider(tmp_path) -> None:
    """No injected provider => enrollment reports 503 (wired in cluster 4)."""
    app = create_app()
    app.dependency_overrides[get_voice_service] = lambda: VoiceProfileService(
        FilesystemVoiceProfileStore(tmp_path / "vp"), RuntimeConfig()
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/voices?display_name=v",
            content=make_wav(),
            headers={"content-type": "audio/wav"},
        )
    assert resp.status_code == 503


def test_auth_required_when_enabled(tmp_path) -> None:
    app = create_app(security=SecurityConfig(auth_enabled=True, auth_token="s3cr3t"))
    app.dependency_overrides[get_voice_service] = lambda: _service(tmp_path)
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/voices?display_name=v",
                content=make_wav(),
                headers={"content-type": "audio/wav"},
            ).status_code
            == 401
        )
        ok = client.post(
            "/v1/voices?display_name=v",
            content=make_wav(),
            headers={"content-type": "audio/wav", "Authorization": "Bearer s3cr3t"},
        )
    assert ok.status_code == 201
