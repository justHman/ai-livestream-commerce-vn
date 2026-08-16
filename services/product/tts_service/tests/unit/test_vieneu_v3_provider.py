"""VieNeu v3 Turbo provider unit tests (Change T tasks 6.1-6.8, 7.2-7.10).

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


class FakeEngine:
    """Deterministic fake of the SDK's internal model engine."""

    def _resolve_style_id(self, style: str) -> int:
        return _VIENEU_STYLES.get(style, 0)


class FakeBatchEngine:
    """Fake of V3TurboBatchEngine: records rows, returns one waveform per row.

    The named scalar params mirror the SDK surface (contract check 7.2 reads
    the signature), so the fake declares them explicitly.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.waveform_len = 48_000 // 10  # 0.1 s @ 48 kHz

    def generate_batch(
        self,
        requests: list[dict],
        *,
        temperature: float = 0.8,
        top_k: int = 25,
        top_p: float = 0.95,
        repetition_penalty: float = 1.2,
        max_new_frames: int = 300,
        use_cudagraph: bool = False,
    ) -> list[np.ndarray]:
        self.calls.append(
            {
                "requests": requests,
                "temperature": temperature,
                "top_k": top_k,
                "top_p": top_p,
                "repetition_penalty": repetition_penalty,
                "max_new_frames": max_new_frames,
                "use_cudagraph": use_cudagraph,
            }
        )
        return [np.zeros(self.waveform_len, dtype=np.float32) for _ in requests]


class FakeTTS:
    """Deterministic fake of the V3TurboVieNeuTTS SDK surface."""

    def __init__(self, backend: str = "pytorch") -> None:
        self.backend = backend
        self.engine = FakeEngine()
        self.batch_engine = FakeBatchEngine() if backend == "pytorch" else None
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
        return self.batch_engine


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


def make_cloned_profile(voice_profile_id: str, tenant_id: str, emb_value: float):
    """Build a cloned VoiceProfile + encoded payload for a ``profile_loader``."""
    from tts.voices.models import VoiceProfile
    from tts.voices.payloads import encode_vieneu_payload

    profile = VoiceProfile(
        voice_profile_id=voice_profile_id,
        tenant_id=tenant_id,
        provider_name="vieneu_v3",
        provider_model_revision="rev",
        profile_kind="cloned",
        display_name=f"clone-{voice_profile_id}",
        provider_payload_location="",
    )
    payload = encode_vieneu_payload(
        model_revision="rev",
        speaker_emb=[emb_value] * 192,
        ref_codes=[emb_value] * 62,
    )
    return profile, payload


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
async def test_synthesize_preset_voice_returns_waveform(fake_tts) -> None:
    provider = make_provider(fake_tts)
    result = await provider.synthesize(make_request(voice_profile_id="default"))
    assert result.sample_rate == SAMPLE_RATE_HZ
    assert result.response_format == "wav"
    assert result.duration_ms == 100
    assert result.waveform is not None


async def test_synthesize_cloned_profile_via_loader(fake_tts) -> None:
    from tts.voices.models import VoiceProfile
    from tts.voices.payloads import encode_vieneu_payload

    profile = VoiceProfile(
        voice_profile_id="vp-1",
        tenant_id="default",  # request tenant (SynthesisRequest.tenant_id)
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
        assert tenant_id == "default"  # SynthesisRequest.tenant_id (not session)
        return profile, payload

    provider = make_provider(fake_tts, profile_loader=loader)
    result = await provider.synthesize(make_request(voice_profile_id="vp-1"))
    assert result.sample_rate == SAMPLE_RATE_HZ
    assert result.duration_ms == 100
    call = fake_tts.infer_calls[-1]
    assert call["voice"]["speaker_emb"].shape == (192,)
    assert call["voice"]["codes"].shape == (62,)


async def test_synthesize_preset_profile_via_loader(fake_tts) -> None:
    from tts.voices.models import VoiceProfile

    profile = VoiceProfile(
        voice_profile_id="vp-preset",
        tenant_id="default",  # request tenant
        provider_name="vieneu_v3",
        provider_model_revision="rev",
        profile_kind="preset",
        display_name=_PRESET_NAME,
        provider_payload_location=f"preset://{_PRESET_NAME}",
    )

    def loader(voice_profile_id: str, tenant_id: str):
        return profile, {}

    provider = make_provider(fake_tts, profile_loader=loader)
    await provider.synthesize(make_request(voice_profile_id="vp-preset"))
    assert fake_tts.infer_calls[-1]["voice"] is not None


async def test_synthesize_profile_not_found(fake_tts) -> None:
    def loader(voice_profile_id: str, tenant_id: str):
        raise ProfileNotFoundError("nope")

    provider = make_provider(fake_tts, profile_loader=loader)
    with pytest.raises(ProfileNotFoundError):
        await provider.synthesize(make_request(voice_profile_id="vp-missing"))


async def test_synthesize_unknown_preset_raises_not_found(fake_tts) -> None:
    from tts.voices.models import VoiceProfile

    profile = VoiceProfile(
        voice_profile_id="vp-ghost",
        tenant_id="default",  # request tenant
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
        await provider.synthesize(make_request(voice_profile_id="vp-ghost"))


async def test_synthesize_inference_error_maps(monkeypatch) -> None:
    class BrokenTTS(FakeTTS):
        def infer(self, text: str, **kwargs):
            raise RuntimeError("engine exploded")

    monkeypatch.setattr("vieneu.Vieneu", lambda mode, **kw: BrokenTTS())
    provider = make_provider(None)
    with pytest.raises(ProviderInferenceError):
        await provider.synthesize(make_request())


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


async def test_unsupported_style_raises_capability_error(fake_tts) -> None:
    provider = make_provider(fake_tts)
    with pytest.raises(CapabilityError):
        await provider.synthesize(make_request(style="vui_ve"))


# ── P1-07: batch-path validation parity with single synthesis ────────────────
async def test_batch_rejects_unsupported_style_before_engine_rows(fake_tts) -> None:
    """P1-07: batch path validates EVERY member before building engine rows.

    An unsupported style must raise the typed CapabilityError (4xx), not a
    KeyError from ``_build_engine_request``, and the batch engine must never
    see the invalid request.
    """
    provider = make_provider(fake_tts)
    with pytest.raises(CapabilityError, match="unsupported style"):
        await provider.synthesize_batch(
            [
                make_request(request_id="req-good", style="natural"),
                make_request(request_id="req-bogus", style="bogus"),
            ]
        )
    assert fake_tts.batch_engine.calls == []


async def test_batch_rejects_unsupported_cue_before_engine_rows(fake_tts) -> None:
    """P1-07: unsupported expressive cue fails the batch with a typed 4xx."""
    provider = make_provider(fake_tts)
    with pytest.raises(CapabilityError, match="unsupported expressive cue"):
        await provider.synthesize_batch(
            [
                make_request(request_id="req-good", input_text="Xin chào"),
                make_request(request_id="req-cue", input_text="Xin chào [hát]"),
            ]
        )
    assert fake_tts.batch_engine.calls == []


async def test_validate_request_exposes_provider_pre_admission_check(fake_tts) -> None:
    """P1-07: the provider exposes a public pre-admission validator.

    ``validate_request`` is the callable the lifespan injects into
    ``AdmissionController`` — it must reject unsupported style/cue with the
    same typed 4xx error the single-synthesis path raises.
    """
    provider = make_provider(fake_tts)
    with pytest.raises(CapabilityError, match="unsupported style"):
        provider.validate_request(make_request(style="bogus"))
    provider.validate_request(make_request(style="natural"))  # no raise


async def test_supported_cue_passes(fake_tts) -> None:
    provider = make_provider(fake_tts)
    result = await provider.synthesize(make_request(input_text="Xin chào [cười]"))
    assert result.duration_ms == 100


async def test_unsupported_cue_raises_capability_error(fake_tts) -> None:
    provider = make_provider(fake_tts)
    with pytest.raises(CapabilityError):
        await provider.synthesize(make_request(input_text="Xin chào [hát]"))


async def test_text_without_brackets_passes(fake_tts) -> None:
    provider = make_provider(fake_tts)
    result = await provider.synthesize(make_request(input_text="Xin chào mọi người"))
    assert result.duration_ms == 100


# ── 7.2: startup batch contract checks ───────────────────────────────────────
def test_init_pytorch_missing_batch_engine_raises(monkeypatch) -> None:
    class NoEngineTTS(FakeTTS):
        def _get_batch_engine(self):
            return None

    monkeypatch.setattr("vieneu.Vieneu", lambda mode, **kw: NoEngineTTS())
    with pytest.raises(ProviderUnavailableError) as exc:
        make_provider(None)
    assert "no batch engine" in str(exc.value)


def test_init_pytorch_engine_surface_failure_raises(monkeypatch) -> None:
    class BoomEngineTTS(FakeTTS):
        def _get_batch_engine(self):
            raise RuntimeError("engine internals exploded")

    monkeypatch.setattr("vieneu.Vieneu", lambda mode, **kw: BoomEngineTTS())
    with pytest.raises(ProviderUnavailableError):
        make_provider(None)


def test_init_onnx_backend_skips_batch_contract(fake_tts) -> None:
    fake_tts.backend = "onnx"
    fake_tts.batch_engine = None
    provider = make_provider(fake_tts)
    assert provider.backend == "onnx"
    assert provider.capabilities().supports_mixed_voice_batch is False


# ── 7.3: batch_key ───────────────────────────────────────────────────────────
def test_batch_key_ignores_voice_and_style(fake_tts) -> None:
    provider = make_provider(fake_tts)
    first = make_request(request_id="req-1", voice_profile_id="preset://A", style="natural")
    second = make_request(request_id="req-2", voice_profile_id="preset://B", style="storytelling")
    assert provider.batch_key(first) == provider.batch_key(second)


def test_batch_key_differs_on_temperature(fake_tts) -> None:
    provider = make_provider(fake_tts)
    first = make_request(generation_config=GenerationConfig(temperature=0.8))
    second = make_request(generation_config=GenerationConfig(temperature=0.5))
    assert provider.batch_key(first) != provider.batch_key(second)


def test_batch_key_is_hashable(fake_tts) -> None:
    provider = make_provider(fake_tts)
    key = provider.batch_key(make_request())
    assert isinstance(key, tuple)
    assert all(isinstance(part, (str, float, int)) for part in key)


# ── 7.4/7.5: mixed-voice batch synthesis ─────────────────────────────────────
async def test_batch_mixed_presets_per_row_voice_and_result_ids(fake_tts) -> None:
    from tts.voices.models import VoiceProfile

    fake_tts.preset_voices["Minh Anh"] = {
        "speaker_emb": np.full(192, 0.5, dtype=np.float32),
        "codes": np.full(62, 5, dtype=np.int64),
    }
    profile_b = VoiceProfile(
        voice_profile_id="vp-preset-b",
        tenant_id="default",  # request tenant
        provider_name="vieneu_v3",
        provider_model_revision="rev",
        profile_kind="preset",
        display_name="Minh Anh",
        provider_payload_location="preset://Minh Anh",
    )

    def loader(voice_profile_id: str, tenant_id: str):
        return profile_b, {}

    provider = make_provider(fake_tts, profile_loader=loader)
    results = await provider.synthesize_batch(
        [
            make_request(request_id="req-1", voice_profile_id="default"),
            make_request(
                request_id="req-2",
                voice_profile_id="vp-preset-b",
                session_id="sess-2",
                utterance_id="utt-2",
                chunk_seq=1,
            ),
        ]
    )
    assert [r.request_id for r in results] == ["req-1", "req-2"]
    assert all(r.waveform is not None for r in results)
    assert all(r.sample_rate == SAMPLE_RATE_HZ for r in results)
    rows = fake_tts.batch_engine.calls[0]["requests"]
    assert rows[0]["speaker_emb"].shape == (192,)
    assert rows[1]["speaker_emb"].shape == (192,)


async def test_batch_mixed_cloned_profiles_per_row_anchor(fake_tts) -> None:
    profiles = {
        "vp-a": make_cloned_profile("vp-a", "default", 0.1),
        "vp-b": make_cloned_profile("vp-b", "default", 0.9),
    }

    def loader(voice_profile_id: str, tenant_id: str):
        return profiles[voice_profile_id]

    provider = make_provider(fake_tts, profile_loader=loader)
    results = await provider.synthesize_batch(
        [
            make_request(request_id="req-1", voice_profile_id="vp-a"),
            make_request(request_id="req-2", voice_profile_id="vp-b"),
        ]
    )
    assert [r.request_id for r in results] == ["req-1", "req-2"]
    rows = fake_tts.batch_engine.calls[0]["requests"]
    assert rows[0]["speaker_emb"][0] == pytest.approx(0.1)
    assert rows[1]["speaker_emb"][0] == pytest.approx(0.9)
    assert rows[0]["ref_codes"][0] == 0
    assert rows[1]["ref_codes"][0] == 0
    assert rows[0]["use_ref_codes"] is True
    assert rows[1]["use_ref_codes"] is True


async def test_batch_mixed_preset_and_cloned(fake_tts) -> None:
    from tts.voices.models import VoiceProfile

    profile_b, payload_b = make_cloned_profile("vp-b", "default", 0.9)
    fake_tts.preset_voices["Minh Anh"] = {
        "speaker_emb": np.full(192, 0.5, dtype=np.float32),
        "codes": np.full(62, 5, dtype=np.int64),
    }
    profile_preset = VoiceProfile(
        voice_profile_id="vp-preset",
        tenant_id="default",
        provider_name="vieneu_v3",
        provider_model_revision="rev",
        profile_kind="preset",
        display_name="Minh Anh",
        provider_payload_location="preset://Minh Anh",
    )

    def loader(voice_profile_id: str, tenant_id: str):
        if voice_profile_id == "vp-b":
            return profile_b, payload_b
        return profile_preset, {}

    provider = make_provider(fake_tts, profile_loader=loader)
    results = await provider.synthesize_batch(
        [
            make_request(request_id="req-1", voice_profile_id="vp-preset"),
            make_request(request_id="req-2", voice_profile_id="vp-b"),
        ]
    )
    assert [r.request_id for r in results] == ["req-1", "req-2"]
    rows = fake_tts.batch_engine.calls[0]["requests"]
    assert rows[0]["speaker_emb"][0] == 0.5
    assert rows[1]["speaker_emb"][0] == pytest.approx(0.9)


async def test_batch_mixed_styles_per_row(fake_tts) -> None:
    provider = make_provider(fake_tts)
    await provider.synthesize_batch(
        [
            make_request(request_id="req-1", style="natural"),
            make_request(request_id="req-2", style="storytelling"),
        ]
    )
    rows = fake_tts.batch_engine.calls[0]["requests"]
    assert rows[0]["style"] == "tu_nhien"
    assert rows[1]["style"] == "doc_truyen"


async def test_batch_cue_phonemes_do_not_leak(fake_tts) -> None:
    provider = make_provider(fake_tts)
    await provider.synthesize_batch(
        [
            make_request(request_id="req-1", input_text="Xin chào [cười]"),
            make_request(request_id="req-2", input_text="Xin chào mọi người"),
        ]
    )
    rows = fake_tts.batch_engine.calls[0]["requests"]
    assert "<|emotion_1|>" in rows[0]["phonemes"]
    assert "<|emotion_1|>" not in rows[1]["phonemes"]


async def test_batch_cardinality_mismatch_raises(fake_tts) -> None:
    class ShortEngine(FakeBatchEngine):
        def generate_batch(
            self,
            requests: list[dict],
            *,
            temperature: float = 0.8,
            top_k: int = 25,
            top_p: float = 0.95,
            repetition_penalty: float = 1.2,
            max_new_frames: int = 300,
            use_cudagraph: bool = False,
        ) -> list[np.ndarray]:
            return [np.zeros(1, dtype=np.float32)]

    fake_tts.batch_engine = ShortEngine()
    provider = make_provider(fake_tts)
    with pytest.raises(ProviderInferenceError):
        await provider.synthesize_batch([make_request(), make_request()])


async def test_batch_mixed_batch_key_raises(fake_tts) -> None:
    provider = make_provider(fake_tts)
    with pytest.raises(ProviderInferenceError):
        await provider.synthesize_batch(
            [
                make_request(generation_config=GenerationConfig(temperature=0.8)),
                make_request(generation_config=GenerationConfig(temperature=0.5)),
            ]
        )


async def test_batch_onnx_falls_back_to_single_path(fake_tts) -> None:
    fake_tts.backend = "onnx"
    fake_tts.batch_engine = None
    provider = make_provider(fake_tts)
    results = await provider.synthesize_batch(
        [
            make_request(request_id="req-1"),
            make_request(request_id="req-2"),
        ]
    )
    assert [r.request_id for r in results] == ["req-1", "req-2"]
    assert len(fake_tts.infer_calls) == 2
    assert fake_tts.batch_engine is None


async def test_batch_empty_returns_no_results(fake_tts) -> None:
    provider = make_provider(fake_tts)
    assert await provider.synthesize_batch([]) == []


# ── NEW-TTS-02: pre-admission profile validation ──────────────────────────────
async def test_validate_request_rejects_missing_profile(fake_tts) -> None:
    """NEW-TTS-02: a nonexistent voice profile must be rejected by
    pre-admission validation (typed 4xx ProfileNotFoundError) so it never
    poisons a mixed-voice batch at inference time."""
    def loader(voice_profile_id: str, tenant_id: str):
        raise ProfileNotFoundError("nope")

    provider = make_provider(fake_tts, profile_loader=loader)
    with pytest.raises(ProfileNotFoundError):
        provider.validate_request(
            make_request(request_id="req-bad", voice_profile_id="vp-missing")
        )


async def test_validate_request_rejects_cross_tenant_profile(fake_tts) -> None:
    """NEW-TTS-02: a cross-tenant profile is invisible at pre-admission
    validation (ProfileNotFoundError), matching the store's tenant scoping."""
    profile, _ = make_cloned_profile("vp-other", "tenant-other", 0.9)

    def loader(voice_profile_id: str, tenant_id: str):
        return profile, {}

    provider = make_provider(fake_tts, profile_loader=loader)
    with pytest.raises(ProfileNotFoundError):
        provider.validate_request(
            make_request(
                request_id="req-bad",
                voice_profile_id="vp-other",
                tenant_id="tenant-a",
            )
        )
