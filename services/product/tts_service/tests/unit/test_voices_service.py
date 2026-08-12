"""VoiceProfileService enrollment orchestration (5.2/5.6/5.7)."""

from __future__ import annotations

import io
import wave

import pytest

from tts.config import RuntimeConfig
from tts.providers.errors import ProfileNotFoundError, ProviderUnavailableError
from tts.voices.enrollment import InvalidReferenceAudioError
from tts.voices.payloads import decode_vieneu_payload
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


class RecordingEnroller:
    """Fake provider enroll callable; records every invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, dict]] = []

    def __call__(self, data: bytes, options: dict) -> dict:
        self.calls.append((data, options))
        return {
            "speaker_emb": [0.1] * 192,
            "ref_codes": [0.2] * 62,
            "style": options["style"],
        }


@pytest.fixture
def service(tmp_path) -> tuple[VoiceProfileService, FilesystemVoiceProfileStore, RecordingEnroller]:
    store = FilesystemVoiceProfileStore(tmp_path / "vp")
    enroller = RecordingEnroller()
    svc = VoiceProfileService(store, RuntimeConfig(voice_max_bytes=10 * 1024 * 1024), enroller)
    return svc, store, enroller


def test_enroll_cloned_persists_payload_once(service) -> None:
    svc, store, enroller = service
    profile = svc.enroll_cloned(make_wav(), tenant_id="t", display_name="Giọng của tôi")
    assert profile.voice_profile_id.startswith("vp_")
    assert profile.profile_kind == "cloned"
    assert profile.tenant_id == "t"

    loaded, payload = store.load_profile(profile.voice_profile_id, "t")
    assert loaded == profile
    decoded = decode_vieneu_payload(payload)
    assert len(decoded["speaker_emb"]) == 192
    assert len(decoded["ref_codes"]) == 62
    assert len(enroller.calls) == 1


def test_enroll_does_not_reencode_on_repeated_load(service) -> None:
    """Loading the same profile many times never re-runs enrollment (5.7)."""
    svc, store, enroller = service
    profile = svc.enroll_cloned(make_wav(), tenant_id="t", display_name="v")
    for _ in range(5):
        reloaded, payload = svc.get_profile_payload(profile.voice_profile_id, "t")
        assert reloaded == profile
        assert decode_vieneu_payload(payload)["speaker_emb"]
    assert len(enroller.calls) == 1


def test_tenant_collision_same_display_name(service) -> None:
    """Same display name in two tenants yields two distinct profiles (5.6)."""
    svc, store, _ = service
    a = svc.enroll_cloned(make_wav(), tenant_id="tenant-a", display_name="Giọng của tôi")
    b = svc.enroll_cloned(make_wav(), tenant_id="tenant-b", display_name="Giọng của tôi")
    assert a.voice_profile_id != b.voice_profile_id
    assert len(store.list_profiles("tenant-a")) == 1
    assert len(store.list_profiles("tenant-b")) == 1


def test_cross_tenant_get_fails(service) -> None:
    svc, _, _ = service
    profile = svc.enroll_cloned(make_wav(), tenant_id="tenant-a", display_name="v")
    with pytest.raises(ProfileNotFoundError):
        svc.get_profile(profile.voice_profile_id, "tenant-b")


def test_restart_reuse_via_new_service(tmp_path) -> None:
    """A fresh service over the same dir reuses the enrolled representation (5.6)."""
    root = tmp_path / "vp"
    store = FilesystemVoiceProfileStore(root)
    svc = VoiceProfileService(store, RuntimeConfig(), RecordingEnroller())
    profile = svc.enroll_cloned(make_wav(), tenant_id="t", display_name="v")

    restarted = VoiceProfileService(FilesystemVoiceProfileStore(root), RuntimeConfig())
    reloaded, payload = restarted.get_profile_payload(profile.voice_profile_id, "t")
    assert reloaded == profile
    assert decode_vieneu_payload(payload)["ref_codes"]


def test_deletion_makes_future_load_fail(service) -> None:
    svc, _, _ = service
    profile = svc.enroll_cloned(make_wav(), tenant_id="t", display_name="v")
    svc.delete_profile(profile.voice_profile_id, "t")
    with pytest.raises(ProfileNotFoundError):
        svc.get_profile(profile.voice_profile_id, "t")


def test_delete_missing_profile_fails(service) -> None:
    svc, _, _ = service
    with pytest.raises(ProfileNotFoundError):
        svc.delete_profile("vp-none", "t")


def test_malformed_audio_rejected_before_provider(service) -> None:
    svc, _, enroller = service
    with pytest.raises(InvalidReferenceAudioError):
        svc.enroll_cloned(b"garbage", tenant_id="t", display_name="v")
    assert len(enroller.calls) == 0  # provider never invoked


def test_oversized_audio_rejected_before_provider(tmp_path) -> None:
    store = FilesystemVoiceProfileStore(tmp_path / "vp")
    enroller = RecordingEnroller()
    cfg_svc = VoiceProfileService(store, RuntimeConfig(voice_max_bytes=100), enroller)
    with pytest.raises(InvalidReferenceAudioError, match="exceeds"):
        cfg_svc.enroll_cloned(make_wav(), tenant_id="t", display_name="v")
    assert len(enroller.calls) == 0


def test_default_enroll_fn_raises_provider_unavailable(tmp_path) -> None:
    """Without an injected provider, enrollment reports 503 semantics (5.2)."""
    store = FilesystemVoiceProfileStore(tmp_path / "vp")
    svc = VoiceProfileService(store, RuntimeConfig())
    with pytest.raises(ProviderUnavailableError, match="not available yet"):
        svc.enroll_cloned(make_wav(), tenant_id="t", display_name="v")
