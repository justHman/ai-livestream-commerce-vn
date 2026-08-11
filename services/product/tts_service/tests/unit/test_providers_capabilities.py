"""Provider capabilities model behavior (Change T task 2.2)."""

from __future__ import annotations

import pytest

from tts.providers.capabilities import ProviderCapabilities


def test_defaults_are_minimal() -> None:
    caps = ProviderCapabilities(provider_name="x", model_revision="r", sample_rate_hz=48_000)
    assert caps.supports_native_batch is False
    assert caps.max_batch_size == 1
    assert caps.supported_styles == ("natural",)


def test_v3_turbo_shaped_capabilities() -> None:
    caps = ProviderCapabilities(
        provider_name="vieneu_v3",
        model_revision="pnnbao-ump/VieNeu-TTS-v3-Turbo",
        sample_rate_hz=48_000,
        supports_native_batch=True,
        max_batch_size=32,
        supports_voice_cloning=True,
        supports_mixed_voice_batch=True,
        supported_styles=("tu_nhien", "vui_ve", "trong_tai"),
        supported_expressive_cues=("laugh", "sigh"),
        supported_response_formats=("pcm", "wav"),
    )
    assert caps.sample_rate_hz == 48_000
    assert caps.supports_mixed_voice_batch


def test_invalid_sample_rate_rejected() -> None:
    with pytest.raises(ValueError, match="sample_rate_hz"):
        ProviderCapabilities(provider_name="x", model_revision="r", sample_rate_hz=0)


def test_invalid_max_batch_size_rejected() -> None:
    with pytest.raises(ValueError, match="max_batch_size"):
        ProviderCapabilities(
            provider_name="x", model_revision="r", sample_rate_hz=48_000, max_batch_size=0
        )


def test_native_batch_requires_style() -> None:
    with pytest.raises(ValueError, match="at least one style"):
        ProviderCapabilities(
            provider_name="x",
            model_revision="r",
            sample_rate_hz=48_000,
            supports_native_batch=True,
            supported_styles=(),
        )
