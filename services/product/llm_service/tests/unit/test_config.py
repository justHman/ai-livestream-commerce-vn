"""Config validation for the LLM self-host service."""

from __future__ import annotations

import pytest

from llm.config import (
    SELF_HOST_ENGINES,
    EngineConfig,
    SecurityConfig,
)


def test_valid_self_host_engines_accepted() -> None:
    for engine in sorted(SELF_HOST_ENGINES):
        ec = EngineConfig(engine=engine, model="Qwen/Qwen3-4B-Instruct")
        assert ec.engine == engine


def test_hosted_openai_compat_rejected_as_engine() -> None:
    with pytest.raises(ValueError, match="not a valid self-host engine"):
        EngineConfig(engine="openai_compat", model="x")


def test_remote_http_rejected_as_engine() -> None:
    with pytest.raises(ValueError, match="not a valid self-host engine"):
        EngineConfig(engine="remote_http")


def test_engine_requires_model() -> None:
    with pytest.raises(ValueError, match="requires LLM_MODEL"):
        EngineConfig(engine="vllm")


def test_security_config_requires_token_when_enabled() -> None:
    with pytest.raises(ValueError, match="LLM_AUTH_TOKEN required"):
        SecurityConfig(auth_enabled=True)


def test_security_config_concurrency_bounds() -> None:
    with pytest.raises(ValueError):
        SecurityConfig(max_concurrent_requests=0)
    with pytest.raises(ValueError):
        SecurityConfig(max_gpu_concurrent_requests=0)


def test_engine_config_to_cfg_dict(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ENGINE", raising=False)
    ec = EngineConfig(engine="transformers", model="m", max_model_len=2048)
    d = ec.to_cfg_dict()
    assert d["engine"] == "transformers"
    assert d["model"] == "m"
    assert d["max_model_len"] == 2048