"""Provider-composition tests (Cluster B task B.1).

Provider-shaped ``*_ADAPTER`` env injected by Terraform must deterministically
compose the backend-owned remote clients through the shared engine seams;
explicit ``*_ENGINE``/``*_BACKEND`` env keeps winning; production refuses local
model engines. All HTTP goes through fakes / httpx MockTransport — no network.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from backend.application.clients.llm.openai_compatible import OpenAICompatibleClient
from backend.application.clients.tts.self_hosted import SelfHostedTTSClient
from backend.application.contracts.llm_engines import (
    ENGINES as LLM_ENGINES,
    LLMRequest,
    load_engine as load_llm_engine,
)
from backend.application.contracts.tts_engines import (
    ENGINES as TTS_ENGINES,
    TTSRequest,
    load_engine as load_tts_engine,
)
from backend.bootstrap.app_factory import create_app
from backend.config import AppConfig, LLMConfig, TTSConfig, _resolve_adapter_defaults

_PROVIDER_VARS = (
    "LLM_ADAPTER",
    "LLM_ENGINE",
    "LLM_BASE_URL",
    "LLM_AUTH_TOKEN",
    "LLM_MODEL",
    "TTS_ADAPTER",
    "TTS_ENGINE",
    "TTS_BASE_URL",
    "TTS_AUTH_TOKEN",
    "AVATAR_ADAPTER",
    "RENDER_BACKEND",
    "ELEVENLABS_API_KEY",
    "TTS_API_KEY",
    "TTS_PRESET_ID",
)


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)


def _composition():
    from backend.application.clients import composition

    composition.ensure_remote_engines_registered()
    return composition


# ---------- 1. LLM_ADAPTER -> openai_compat ----------


def test_llm_adapter_env_composes_openai_compat_client(monkeypatch):
    monkeypatch.setenv("LLM_ADAPTER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "http://fake")
    composition = _composition()

    constructed: list[dict] = []
    seen_requests: list = []

    class FakeLLMClient:
        def __init__(self, base_url="", *, api_key="", model=""):
            constructed.append({"base_url": base_url, "api_key": api_key, "model": model})

        def chat(self, req):
            seen_requests.append(req)
            return SimpleNamespace(
                text="chao ban",
                finish_reason="stop",
                num_prompt_tokens=3,
                num_generated_tokens=4,
            )

        def chat_stream(self, req):
            yield from ("xin ", "chao")

        def close(self):
            pass

    monkeypatch.setattr(composition, "OpenAICompatibleClient", FakeLLMClient)

    cfg = AppConfig.from_env().llm.to_engine_cfg()
    assert cfg["engine"] == "openai_compat"

    engine = load_llm_engine(cfg)

    assert isinstance(engine, composition.OpenAICompatLLMEngine)
    assert constructed == [{"base_url": "http://fake", "api_key": "", "model": ""}]

    resp = engine.generate(
        LLMRequest(messages=[{"role": "user", "content": "xin chao"}], max_tokens=64)
    )
    assert resp.text == "chao ban"
    assert resp.engine == "openai_compat"
    req = seen_requests[0]
    assert [m.content for m in req.messages] == ["xin chao"]
    assert req.max_tokens == 64


def test_openai_compat_engine_generate_maps_request_over_mock_transport():
    """Real client + fake transport: the adapter maps LLMRequest faithfully."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["messages"] == [{"role": "user", "content": "xin chao"}]
        assert body["max_tokens"] == 32
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 5},
            },
        )

    composition = _composition()
    client = OpenAICompatibleClient(
        base_url="http://fake",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    engine = composition.OpenAICompatLLMEngine(client)

    resp = engine.generate(
        LLMRequest(messages=[{"role": "user", "content": "xin chao"}], max_tokens=32)
    )

    assert resp.text == "ok"
    assert resp.num_prompt_tokens == 2
    assert resp.num_generated_tokens == 5


# ---------- 2. TTS_ADAPTER=self_hosted -> remote_http ----------


def test_tts_self_hosted_adapter_resolves_remote_http_engine(monkeypatch):
    monkeypatch.setenv("TTS_ADAPTER", "self_hosted")
    monkeypatch.setenv("TTS_BASE_URL", "http://fake")
    composition = _composition()

    cfg = AppConfig.from_env().tts.to_engine_cfg()

    assert cfg["engine"] == "remote_http"
    engine = load_tts_engine(cfg)
    assert isinstance(engine, composition.RemoteHttpTTSEngine)


def test_remote_http_synthesize_maps_pcm16_bytes_and_sample_rate():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/speech"
        return httpx.Response(
            200,
            headers={"x-audio-sample-rate": "16000"},
            content=b"\x01\x00\xff\x7f",
        )

    composition = _composition()
    client = SelfHostedTTSClient(
        base_url="http://fake",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    engine = composition.RemoteHttpTTSEngine(client, sample_rate=16000)

    chunk = engine.synthesize(TTSRequest(text="xin chao"))

    assert chunk.sample_rate == 16000
    expected = np.frombuffer(b"\x01\x00\xff\x7f", dtype="<i2").astype(np.float32) / 32767.0
    np.testing.assert_allclose(chunk.pcm, expected)


# ---------- 3. TTS_ADAPTER=elevenlabs ----------


def test_tts_elevenlabs_adapter_selects_engine_and_constructs_client_with_fake_key(
    monkeypatch,
):
    monkeypatch.setenv("TTS_ADAPTER", "elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    composition = _composition()

    recorded: dict = {}

    class FakeElevenLabsClient:
        def __init__(self, *, api_key="", **kwargs):
            recorded["api_key"] = api_key
            recorded.update(kwargs)

        def synthesize(self, text):
            return SimpleNamespace(pcm=np.zeros(4, dtype=np.float32), sample_rate=24000)

        def close(self):
            pass

    monkeypatch.setattr(composition, "ElevenLabsTTSClient", FakeElevenLabsClient)

    cfg = AppConfig.from_env().tts.to_engine_cfg()
    assert cfg["engine"] == "elevenlabs"

    engine = load_tts_engine(cfg)

    assert isinstance(engine, composition.ElevenLabsTTSEngine)
    assert recorded["api_key"] == "fake-key"
    chunk = engine.synthesize(TTSRequest(text="hi"))
    assert chunk.sample_rate == 24000


def test_tts_openai_speech_adapter_selector(monkeypatch):
    monkeypatch.setenv("TTS_ADAPTER", "openai_speech")
    composition = _composition()

    engine = load_tts_engine(AppConfig.from_env().tts.to_engine_cfg())

    assert isinstance(engine, composition.OpenAISpeechTTSEngine)


# ---------- 4. explicit env wins over adapter derivation ----------


def test_explicit_llm_engine_none_wins_over_llm_adapter(monkeypatch):
    monkeypatch.setenv("LLM_ADAPTER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "http://fake")
    monkeypatch.setenv("LLM_ENGINE", "none")

    derived = _resolve_adapter_defaults(dict(os.environ))

    assert derived["llm_engine"] is None
    assert AppConfig.from_env().llm.engine == "none"


def test_explicit_render_backend_wins_over_avatar_adapter(monkeypatch):
    monkeypatch.setenv("AVATAR_ADAPTER", "liveavatar")
    monkeypatch.setenv("RENDER_BACKEND", "mock")

    assert AppConfig.from_env().render_backend == "mock"


def test_avatar_adapter_self_hosted_leaves_render_resolution_untouched(monkeypatch):
    monkeypatch.setenv("AVATAR_ADAPTER", "self_hosted")

    assert _resolve_adapter_defaults({"AVATAR_ADAPTER": "self_hosted"})["render_backend"] is None
    assert AppConfig.from_env().render_backend == "cloud_liveavatar"


# ---------- 5. no adapter vars => current defaults byte-for-byte ----------


def test_no_adapter_vars_preserves_current_defaults():
    assert _resolve_adapter_defaults({}) == {
        "llm_engine": None,
        "tts_engine": None,
        "render_backend": None,
    }

    config = AppConfig.from_env()

    assert config.llm.engine == "none"
    assert config.tts.engine == "transformers"
    assert config.render_backend == "cloud_liveavatar"


# ---------- 6. production guard forbids local model engines ----------


def test_production_rejects_local_llm_engine():
    config = AppConfig(
        app_env="prod",
        cors_origins="https://shop.example",
        llm=LLMConfig(engine="vllm"),
    )

    with pytest.raises(RuntimeError, match="vllm"):
        create_app(config=config)


def test_production_rejects_local_tts_engine():
    config = AppConfig(
        app_env="prod",
        cors_origins="https://shop.example",
        tts=TTSConfig(engine="transformers"),
    )

    with pytest.raises(RuntimeError, match="transformers"):
        create_app(config=config)


# ---------- 7. registration is idempotent ----------


def test_ensure_remote_engines_registered_is_idempotent():
    from backend.application.clients.composition import ensure_remote_engines_registered

    composition = _composition()
    ensure_remote_engines_registered()  # second call must not duplicate

    assert LLM_ENGINES["openai_compat"] is composition.OpenAICompatLLMEngine
    assert list(LLM_ENGINES).count("openai_compat") == 1
    tts_adapters = {
        "remote_http": composition.RemoteHttpTTSEngine,
        "elevenlabs": composition.ElevenLabsTTSEngine,
        "openai_speech": composition.OpenAISpeechTTSEngine,
    }
    for name, cls in tts_adapters.items():
        assert TTS_ENGINES[name] is cls
        assert list(TTS_ENGINES).count(name) == 1
