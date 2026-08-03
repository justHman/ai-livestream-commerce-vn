"""Config validation for the TTS self-host service."""

from __future__ import annotations

import pytest

from tts.config import (
    SELF_HOST_ENGINES,
    EngineConfig,
    SecurityConfig,
)


def test_valid_self_host_engines_accepted() -> None:
    for engine in sorted(SELF_HOST_ENGINES):
        ec = EngineConfig(engine=engine, model="x")
        assert ec.engine == engine


def test_remote_http_rejected_as_engine() -> None:
    with pytest.raises(ValueError, match="not a valid self-host engine"):
        EngineConfig(engine="remote_http")


def test_elevenlabs_rejected_as_engine() -> None:
    with pytest.raises(ValueError, match="not a valid self-host engine"):
        EngineConfig(engine="elevenlabs")


def test_openai_speech_rejected_as_engine() -> None:
    with pytest.raises(ValueError, match="not a valid self-host engine"):
        EngineConfig(engine="openai_speech")


def test_security_config_requires_token_when_enabled() -> None:
    with pytest.raises(ValueError, match="required when"):
        SecurityConfig(auth_enabled=True)


def test_engine_config_maps_to_cfg_dict() -> None:
    ec = EngineConfig(engine="cosyvoice", model="m", sample_rate=48_000)
    d = ec.to_cfg_dict()
    assert d["engine"] == "cosyvoice"
    assert d["sample_rate"] == 48_000


def test_invalid_sample_rate() -> None:
    with pytest.raises(ValueError):
        EngineConfig(engine="vieneu", sample_rate=0)