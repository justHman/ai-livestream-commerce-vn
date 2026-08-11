"""VieNeu v3 Turbo provider unit tests (Change T tasks 6.1-6.8).

The SDK is a system boundary: tests monkeypatch the ``vieneu.Vieneu``
factory with a deterministic fake TTS object. No real model is ever loaded.
"""

from __future__ import annotations

import numpy as np
import pytest

from tts.config import RuntimeConfig
from tts.providers.errors import (
    CapabilityError,
    ProfileNotFoundError,
    ProviderInferenceError,
    ProviderUnavailableError,
)
from tts.providers.models import GenerationConfig, Priority, SynthesisRequest
from tts.providers.vieneu_v3 import (
    CANONICAL_STYLES,
    SUPPORTED_CUES,
    SAMPLE_RATE_HZ,
    _VIENEU_STYLES,
    VieNeuV3TurboProvider,
)

_PRESET_NAME = "Phạm Tuyên"
_PRESET_PAYLOAD = {
    "speaker_emb": np.zeros(192, dtype=np.float32),
    "codes": np.zeros(62, dtype=np.int64),
}


class FakeTTS:
    """Deterministic fake of the V3TurboVieNeuTTS SDK surface."""

    def __init__(self, backend: str = "pytorch") -> None:
        self.backend = backend
        self.infer_calls: list[dict] = []
        self.preset_voices: dict[str, dict] = {_PRESET_NAME: dict(_PRESET_PAYLOAD)}
        self._default_voice = _PRESET_NAME

    def get_preset_voice(self, name: str | None = None) -> dict:
        voice = name or self._default_voice
        if voice not in self.preset_voices:
            raise ValueError(f"Voice '{voice}' not found")
        return self.preset_voices[voice]

    def infer(self, text: str, **kwargs) -> np.ndarray:
        self.infer_calls.append({"text": text, **kwargs})
        return np.zeros(48_000 // 10, dtype=np.float32)  # 0.1 s @ 48 kHz

    def _get_batch_engine(self):
        return None


@pytest.fixture
def fake_tts(monkeypatch):
    tts = FakeTTS()
    monkeypatch.setattr("vieneu.Vieneu", lambda mode, **kw: tts)
    return tts


def make_request(**overrides) -> SynthesisRequest:
    fields = dict(
        request_id="req-1",
        session_id="sess-1",
        utterance_id="utt-1",
        chunk_seq=0,
        input_text="Xin chào",
        voice_profile_id="default",
        style="natural",
        priority=Priority.NORMAL,
        response_format="wav",
        generation_config=GenerationConfig(),
    )
    fields.update(overrides)
    return SynthesisRequest(**fields)


def make_provider(fake_tts, config: RuntimeConfig | None = None, profile_loader=None):
    return VieNeuV3TurboProvider(config or RuntimeConfig(), profile_loader)


# ── 6.1/6.2: init, backend detection, capabilities ─────────────────────────
def test_init_auto_uses_sdk_factory(fake_tts) -> None:
    provider = make_provider(fake_tts)
    assert provider.backend == "pytorch"


def test_init_forced_cpu_maps_to_cpu_device(fake_tts) -> None:
    provider = make_provider(fake_tts, RuntimeConfig(accelerator="cpu"))
    assert provider.backend == "pytorch"


def test_init_gpu_without_cuda_raises_clear_error(monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("no CUDA runtime found")

    monkeypatch.setattr("vieneu.Vieneu", boom)
    with pytest.raises(ProviderUnavailableError) as exc:
        make_provider(None, RuntimeConfig(accelerator="gpu"))
    assert "forced GPU" in str(exc.value)


def test_init_failure_raises_provider_unavailable(monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("model registry probe failed")

    monkeypatch.setattr("vieneu.Vieneu", boom)
    with pytest.raises(ProviderUnavailableError):
        make_provider(None)


def test_capabilities_pytorch_backend(fake_tts) -> None:
    caps = make_provider(fake_tts).capabilities()
    assert caps.provider_name == "vieneu_v3"
    assert caps.sample_rate_hz == SAMPLE_RATE_HZ
    assert caps.supports_native_batch is True
    assert caps.max_batch_size == 32
    assert caps.supports_voice_cloning is True
    assert caps.supports_mixed_voice_batch is True
    assert caps.supported_styles == CANONICAL_STYLES
    assert caps.supported_expressive_cues == SUPPORTED_CUES
    assert caps.supported_response_formats == ("pcm", "wav")


def test_capabilities_onnx_backend_single_batch(fake_tts) -> None:
    fake_tts.backend = "onnx"
    caps = make_provider(fake_tts).capabilities()
    assert caps.supports_native_batch is False
    assert caps.max_batch_size == 1
    assert caps.supports_mixed_voice_batch is False


# ── 6.4/6.5: profile resolution and synthesis ──────────────────────────────
def test_synthesize_preset_voice_returns_waveform(fake_tts) -> None:
    provider = make_provider(fake_tts)
    result = provider.synthesize(make_request(voice_profile_id="default"))
    assert result.sample_rate == SAMPLE_RATE_HZ
    assert result.response_format == "wav"
    assert result.duration_ms == 100
    assert result.waveform is not None


def test_synthesize_cloned_profile_via_loader(fake_tts) -> None:
    from tts.voices.models import VoiceProfile
    from tts.voices.payloads import encode_vieneu_payload

    profile = VoiceProfile(
        voice_profile_id="vp-1",
        tenant_id="sess-1",
        provider_name="vieneu_v3",
        provider_model_revision="rev",
        profile_kind="cloned",
        display_name="clone",
        provider_payload_location="",
    )
    payload = encode_vieneu_payload(
        model_revision="rev", speaker_emb=[0.1] * 192, ref_codes=[0.2] * 62
    )

    def loader(voice_profile_id: str, tenant_id: str):
        assert voice_profile_id == "vp-1"
        assert tenant_id == "sess-1"
        return profile, payload

    provider = make_provider(fake_tts, profile_loader=loader)
    result = provider.synthesize(make_request(voice_profile_id="vp-1"))
    assert result.sample_rate == SAMPLE_RATE_HZ
    assert result.duration_ms == 100
    call = fake_tts.infer_calls[-1]
    assert call["voice"]["speaker_emb"].shape == (192,)
    assert call["voice"]["codes"].shape == (62,)


def test_synthesize_preset_profile_via_loader(fake_tts) -> None:
    from tts.voices.models import VoiceProfile

    profile = VoiceProfile(
        voice_profile_id="vp-preset",
        tenant_id="sess-1",
        provider_name="vieneu_v3",
        provider_model_revision="rev",
        profile_kind="preset",
        display_name=_PRESET_NAME,
        provider_payload_location=f"preset://{_PRESET_NAME}",
    )

    def loader(voice_profile_id: str, tenant_id: str):
        return profile, {}

    provider = make_provider(fake_tts, profile_loader=loader)
    provider.synthesize(make_request(voice_profile_id="vp-preset"))
    assert fake_tts.infer_calls[-1]["voice"] is not None


def test_synthesize_profile_not_found(fake_tts) -> None:
    def loader(voice_profile_id: str, tenant_id: str):
        raise ProfileNotFoundError("nope")

    provider = make_provider(fake_tts, profile_loader=loader)
    with pytest.raises(ProfileNotFoundError):
        provider.synthesize(make_request(voice_profile_id="vp-missing"))


def test_synthesize_unknown_preset_raises_not_found(fake_tts) -> None:
    from tts.voices.models import VoiceProfile

    profile = VoiceProfile(
        voice_profile_id="vp-ghost",
        tenant_id="sess-1",
        provider_name="vieneu_v3",
        provider_model_revision="rev",
        profile_kind="preset",
        display_name="Ghost",
        provider_payload_location="preset://Ghost",
    )

    def loader(voice_profile_id: str, tenant_id: str):
        return profile, {}

    provider = make_provider(fake_tts, profile_loader=loader)
    with pytest.raises(ProfileNotFoundError):
        provider.synthesize(make_request(voice_profile_id="vp-ghost"))


def test_synthesize_inference_error_maps(monkeypatch) -> None:
    class BrokenTTS(FakeTTS):
        def infer(self, text: str, **kwargs):
            raise RuntimeError("engine exploded")

    monkeypatch.setattr("vieneu.Vieneu", lambda mode, **kw: BrokenTTS())
    provider = make_provider(None)
    with pytest.raises(ProviderInferenceError):
        provider.synthesize(make_request())


# ── 6.6: style and expressive cue validation ───────────────────────────────
@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("natural", "tu_nhien"),
        ("news", "tin_tuc"),
        ("tin_tuc", "tin_tuc"),
        ("storytelling", "doc_truyen"),
        ("doc_truyen", "doc_truyen"),
    ],
)
def test_style_aliases_map_to_vieneu(fake_tts, style, expected) -> None:
    assert _VIENEU_STYLES[style] == expected


def test_unsupported_style_raises_capability_error(fake_tts) -> None:
    provider = make_provider(fake_tts)
    with pytest.raises(CapabilityError):
        provider.synthesize(make_request(style="vui_ve"))


def test_supported_cue_passes(fake_tts) -> None:
    provider = make_provider(fake_tts)
    result = provider.synthesize(make_request(input_text="Xin chào [cười]"))
    assert result.duration_ms == 100


def test_unsupported_cue_raises_capability_error(fake_tts) -> None:
    provider = make_provider(fake_tts)
    with pytest.raises(CapabilityError):
        provider.synthesize(make_request(input_text="Xin chào [hát]"))


def test_text_without_brackets_passes(fake_tts) -> None:
    provider = make_provider(fake_tts)
    result = provider.synthesize(make_request(input_text="Xin chào mọi người"))
    assert result.duration_ms == 100
