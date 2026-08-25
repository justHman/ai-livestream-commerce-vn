"""Provider credential parity + production fail-loud tests (Cluster B task B.4).

R2.3: the backend's outbound clients read the internal-service credential as
``LLM_AUTH_TOKEN`` / ``TTS_AUTH_TOKEN``, so production startup validates that an
enabled remote/provider engine carries a real (non-empty, non-placeholder)
credential and base URL before the container boots. R8.4: a configured
real-provider engine that fails to load must fail startup in production instead
of silently degrading to echo/tone stubs; dev/CI (APP_ENV=dev) keeps the stub
fallback.
"""

from __future__ import annotations

import pytest

import backend.engine_manager as engine_manager_module
from backend.bootstrap.app_factory import create_app
from backend.config import AppConfig, LLMConfig, TTSConfig

_CREDENTIAL_VARS = (
    "LLM_AUTH_TOKEN",
    "TTS_AUTH_TOKEN",
    "TTS_API_KEY",
    "ELEVENLABS_API_KEY",
    "LLM_ADAPTER",
    "LLM_ENGINE",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "TTS_ADAPTER",
    "TTS_ENGINE",
    "TTS_BASE_URL",
    "TTS_PRESET_ID",
    "AVATAR_ADAPTER",
    "RENDER_BACKEND",
)


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)


def _prod_llm_config() -> AppConfig:
    # tts=tone (stub) so the production local-engine guard doesn't fire; this
    # test targets the LLM credential validation only.
    return AppConfig(
        app_env="prod",
        cors_origins="https://shop.example",
        # Real auth tokens: the B.5 production guard rejects empty/placeholder
        # BACKEND_API_TOKEN/ADMIN_API_TOKEN; this fixture targets the provider
        # credential validation, not the auth plane.
        backend_api_token="real-viewer",
        admin_api_token="real-admin",
        llm=LLMConfig(engine="openai_compat", base_url="http://llm.internal/v1"),
        tts=TTSConfig(engine="tone"),
    )


def test_production_openai_compat_requires_credential(monkeypatch) -> None:
    monkeypatch.delenv("LLM_AUTH_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="LLM_AUTH_TOKEN"):
        create_app(config=_prod_llm_config())


def test_production_rejects_placeholder_llm_credential(monkeypatch) -> None:
    monkeypatch.setenv("LLM_AUTH_TOKEN", "CHANGE_ME")

    with pytest.raises(RuntimeError, match="placeholder"):
        create_app(config=_prod_llm_config())


def test_production_remote_http_requires_tts_credential(monkeypatch) -> None:
    monkeypatch.delenv("TTS_AUTH_TOKEN", raising=False)
    config = AppConfig(
        app_env="prod",
        cors_origins="https://shop.example",
        tts=TTSConfig(engine="remote_http", base_url="http://tts.internal/v1"),
    )

    with pytest.raises(RuntimeError, match="TTS_AUTH_TOKEN"):
        create_app(config=config)


def test_production_engine_load_failure_reraises(monkeypatch) -> None:
    """R8.4: a configured real provider that fails to load fails startup."""
    monkeypatch.setenv("LLM_AUTH_TOKEN", "real-secret")  # credential guard passes first

    def _boom(_cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine_manager_module, "load_llm_engine", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        create_app(config=_prod_llm_config())


def test_dev_engine_load_failure_keeps_stub_fallback(monkeypatch) -> None:
    """Dev/CI keeps today's stub fallback: record error, boot with echo stub."""
    monkeypatch.setenv("LLM_AUTH_TOKEN", "real-secret")

    def _boom(_cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine_manager_module, "load_llm_engine", _boom)
    config = AppConfig(
        app_env="dev",
        llm=LLMConfig(engine="openai_compat", base_url="http://llm.internal/v1"),
        tts=TTSConfig(engine="tone"),
    )

    app = create_app(config=config)
    manager = app.state.container.engine_manager
    assert manager.llm is None  # stub fallback
    assert manager.llm_load_error is not None
    assert "boom" in manager.llm_load_error
