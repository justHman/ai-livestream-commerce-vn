"""Provider-neutral runtime config validation (Change T tasks 1.5/1.6)."""

from __future__ import annotations

import pytest

from tts.config import (
    ACCELERATORS,
    DEFAULT_TTS_MODEL_REVISION,
    DEFAULT_TTS_PROVIDER,
    RESPONSE_FORMATS,
    RuntimeConfig,
    load_runtime_config,
)


def test_defaults_are_provider_neutral_and_v3() -> None:
    cfg = RuntimeConfig()
    assert cfg.provider == DEFAULT_TTS_PROVIDER
    assert cfg.model_revision == DEFAULT_TTS_MODEL_REVISION
    assert cfg.accelerator == "auto"
    assert cfg.response_format == "wav"
    assert cfg.global_pending_limit == 512
    assert cfg.per_session_pending_limit == 64
    assert cfg.request_deadline_ms == 30_000
    assert cfg.max_batch_size == 32
    assert cfg.coalesce_window_ms == 10


def test_voice_store_uri_defaults_under_runtime_root() -> None:
    cfg = load_runtime_config()
    assert cfg.voice_store_uri.startswith("file://")
    assert cfg.voice_store_uri.endswith("/voice_profiles")


def test_accelerators_accepted() -> None:
    for accel in ACCELERATORS:
        assert RuntimeConfig(accelerator=accel).accelerator == accel


def test_invalid_accelerator_rejected() -> None:
    with pytest.raises(ValueError, match="TTS_ACCELERATOR"):
        RuntimeConfig(accelerator="tpu")


def test_empty_provider_rejected() -> None:
    with pytest.raises(ValueError, match="TTS_PROVIDER"):
        RuntimeConfig(provider="")


def test_empty_model_revision_rejected() -> None:
    with pytest.raises(ValueError, match="TTS_MODEL_REVISION"):
        RuntimeConfig(model_revision="")


def test_invalid_response_format_rejected() -> None:
    with pytest.raises(ValueError, match="TTS_RESPONSE_FORMAT"):
        RuntimeConfig(response_format="mp3")


def test_response_formats_accepted() -> None:
    for fmt in RESPONSE_FORMATS:
        assert RuntimeConfig(response_format=fmt).response_format == fmt


@pytest.mark.parametrize(
    ("field", "bad_value", "env_name"),
    [
        ("global_pending_limit", 0, "TTS_GLOBAL_PENDING_LIMIT"),
        ("per_session_pending_limit", 0, "TTS_PER_SESSION_PENDING_LIMIT"),
        ("request_deadline_ms", 0, "TTS_REQUEST_DEADLINE_MS"),
        ("max_batch_size", 0, "TTS_MAX_BATCH_SIZE"),
        ("coalesce_window_ms", 0, "TTS_COALESCE_WINDOW_MS"),
    ],
)
def test_scheduler_bounds_must_be_positive(field: str, bad_value: int, env_name: str) -> None:
    with pytest.raises(ValueError, match=env_name):
        RuntimeConfig(**{field: bad_value})
