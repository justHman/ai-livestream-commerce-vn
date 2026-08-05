"""Unit tests for the FastAPI app factory + split health endpoints (Task 6).

Verifies:
  - ``create_app()`` (default, env-driven) returns a FastAPI app with the
    expected title and works under ``RENDER_BACKEND=mock`` + empty
    ``LIVEAVATAR_API_KEY``.
  - ``create_app(deps=...)`` accepts injected dependencies (no heavy model
    load) and wires them into the router.
  - ``/health/live`` is always 200 with ``{"ok": True, "status": "live"}``.
  - ``/health/ready`` reports backend readiness (200) with the active
    ``render_backend`` / ``llm_engine`` / ``tts_engine`` fields.
  - Importing ``backend.main`` with mock + empty key does NOT raise and
    exposes a module-level ``app`` (FastAPI) + ``create_app`` callable.
  - The existing ``/health`` route is preserved.
  - Finding 1: mock mode with a configured real LLM/TTS engine loads it
    (engine_manager.llm/.tts not None); only cloud-specific wiring is skipped.
  - Finding 2: /health/ready reports not-ready (ok=False) with the error
    surfaced when a configured real engine failed to load.

All tests offline — no ``LIVEAVATAR_API_KEY``, no ``REDIS_URL``, no GPU, no
model downloads. Uses ``fastapi.testclient.TestClient`` for real HTTP calls.

Migrated from ``core/tests/test_app_factory.py`` (OpenSpec 1.50) — the
canonical backend service is the owner; core imports replaced with
``backend.*`` / ``llm.*`` / ``tts.*``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.application.director.embeddings import HashingEmbedder
from backend.config import AppConfig
from backend.engine_manager import EngineManager  # noqa: F401
from conftest import make_deps as _Deps  # noqa: F401


# ---------- fixtures ----------


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env that lets the module-level app boot without a cloud key."""
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")


@pytest.fixture
def injected_deps():
    """A fully-injected deps object (mock backend + empty EngineManager).

    ``create_app(deps=...)`` mirrors it into the typed BootstrapContainer
    without loading engines.
    """
    from backend.engine_manager import EngineManager

    deps = _Deps(engine_manager=EngineManager())
    deps.config = None
    return deps


# ---------- create_app() default (env-driven) ----------


def test_create_app_default_returns_fastapi_with_expected_title(mock_env: None) -> None:
    from backend.main import create_app

    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.title == "VN Live-Commerce Host — core API"


def test_create_app_wires_livekit_registry_only_when_publishing_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("LIVEKIT_PUBLISH", "1")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-secret-32-characters-harmless")

    from backend.main import create_app

    app = create_app(config=AppConfig.from_env())

    assert app.state.container.livekit_publishers is not None


def test_module_level_app_is_fastapi_and_create_app_callable(mock_env: None) -> None:
    """Importing backend.main must not raise; module-level `app` must exist."""
    import importlib

    import backend.main as server_mod

    # Re-import under the mock env to make sure the module-level app was built
    # against RENDER_BACKEND=mock (idempotent if already imported).
    importlib.reload(server_mod)
    assert isinstance(server_mod.app, FastAPI)
    assert callable(server_mod.create_app)


# ---------- /health/live + /health/ready ----------


def test_health_live_always_200(mock_env: None, injected_deps) -> None:
    from backend.main import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/live")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "live"


def test_health_ready_200_mock_backend(mock_env: None, injected_deps) -> None:
    from backend.main import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["render_backend"] == "mock"
    # No llm/tts loaded on the injected EngineManager -> "none" / "tone" stubs.
    assert body["llm_engine"] in ("none", None, "")
    assert body["tts_engine"] in ("tone", None, "")


def test_health_existing_route_preserved(mock_env: None, injected_deps) -> None:
    from backend.main import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["render_backend"] == "mock"


# ---------- root route ----------


def test_root_route_returns_render_backend(mock_env: None, injected_deps) -> None:
    from backend.main import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "vn-live-commerce-host"
    assert body["render_backend"] == "mock"
    assert body["engine_manager"] is True


# ---------- injected deps skip model loading ----------


def test_injected_deps_engine_manager_used_as_is(mock_env: None, injected_deps) -> None:
    """create_app(deps=...) must NOT call engine_mgr.load_llm/load_tts.

    We assert by inspecting the injected EngineManager after boot: it should
    still have no llm/tts loaded (``engine_manager.llm is None`` and
    ``engine_manager.tts is None``). If create_app had loaded models, those
    attributes would be non-None (or the call would have failed offline).
    """
    from backend.main import create_app

    app = create_app(deps=injected_deps)
    em: EngineManager = injected_deps.engine_manager
    assert em.llm is None, "create_app(deps=...) must not load an LLM engine"
    assert em.tts is None, "create_app(deps=...) must not load a TTS engine"
    # Sanity: the app is still a FastAPI app.
    assert isinstance(app, FastAPI)


def test_health_ready_reports_llm_none_when_not_loaded(mock_env: None, injected_deps) -> None:
    """/health/ready on injected (no-model) deps reports llm_engine 'none'."""
    from backend.main import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    body = r.json()
    assert r.status_code == 200
    assert body["llm_engine"] == "none"
    assert body["tts_engine"] == "tone"
    assert body["ok"] is True


# ---------- Finding 1: mock mode loads configured real LLM/TTS engines ----------


def test_mock_mode_loads_configured_llm_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 1: RENDER_BACKEND=mock + a registered LLM engine → engine_mgr.llm
    is not None after create_app (env-driven path). The cloud import +
    reconfigure_cloud are skipped, but the LLM/TTS engines are loaded."""
    from llm.engines.base import LLMEngine, LLMRequest, LLMResponse, register_engine  # noqa: F401

    @register_engine("mock-test-llm")
    class _MockTestLLM(LLMEngine):
        name = "mock-test-llm"

        def __init__(self) -> None:
            pass

        @classmethod
        def from_config(cls, cfg: dict) -> "_MockTestLLM":
            return cls()

        def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
            return LLMResponse(text="ok", engine=self.name)

    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "mock-test-llm")
    monkeypatch.setenv("TTS_ENGINE", "tone")  # stub → no load
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")

    from backend.main import create_app

    # Build a fresh AppConfig from the patched env so the test sees the
    # monkeypatched values (the module-level CONFIG was built at import time).
    app = create_app(config=AppConfig.from_env())
    em = app.state.container.engine_manager
    assert em is not None
    assert em.llm is not None, (
        "Finding 1: mock mode with a configured real LLM engine must load it "
        "(em.llm should be the _MockTestLLM instance, not None)"
    )
    assert em.llm.name == "mock-test-llm"
    # TTS_ENGINE=tone → stub → no load → tts is None (stub path).
    assert em.tts is None
    # No load failure recorded.
    assert em.llm_load_error is None
    assert em.tts_load_error is None


def test_mock_mode_loads_configured_tts_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 1: RENDER_BACKEND=mock + a registered TTS engine → engine_mgr.tts
    is not None after create_app (env-driven path)."""
    from tts.engines.base import AudioChunk, TTSRequest, TTSEngine, register_engine  # noqa: F401
    import numpy as np

    @register_engine("mock-test-tts")
    class _MockTestTTS(TTSEngine):
        name = "mock-test-tts"
        sample_rate = 24000

        def __init__(self) -> None:
            pass

        @classmethod
        def from_config(cls, cfg: dict) -> "_MockTestTTS":
            e = cls()
            e.sample_rate = int(cfg.get("sample_rate", 24000))
            return e

        def synthesize(self, req: TTSRequest) -> AudioChunk:  # pragma: no cover
            return AudioChunk(pcm=np.zeros(8, dtype=np.float32), sample_rate=self.sample_rate)

    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")  # stub → no load
    monkeypatch.setenv("TTS_ENGINE", "mock-test-tts")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")

    from backend.main import create_app

    app = create_app(config=AppConfig.from_env())
    em = app.state.container.engine_manager
    assert em is not None
    assert em.tts is not None, (
        "Finding 1: mock mode with a configured real TTS engine must load it "
        "(em.tts should be the _MockTestTTS instance, not None)"
    )
    assert em.tts.name == "mock-test-tts"
    # LLM_ENGINE=none → stub → no load → llm is None (stub path).
    assert em.llm is None
    # No load failure recorded.
    assert em.llm_load_error is None
    assert em.tts_load_error is None


def test_mock_mode_with_default_stubs_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline-test safety: LLM_ENGINE=none + TTS_ENGINE=tone + RENDER_BACKEND=mock
    (the defaults) → create_app still starts, em.llm/.tts stay None, /lite/say
    works via the _NoopEngine/ToneEngine fallback in _streaming_say."""
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")

    from backend.main import create_app

    app = create_app(config=AppConfig.from_env())
    em = app.state.container.engine_manager
    assert em is not None
    # Defaults → stubs → no load → None.
    assert em.llm is None
    assert em.tts is None
    assert em.llm_load_error is None
    assert em.tts_load_error is None
    # /health/ready is ready (mock backend, no configured real engine).
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    body = r.json()
    assert body["ok"] is True
    assert body["render_backend"] == "mock"


# ---------- Finding 2: /health/ready honest on engine load failure ----------


def test_health_ready_not_ready_with_hash_embedder_outside_dev(
    monkeypatch: pytest.MonkeyPatch, injected_deps
) -> None:
    from backend.application.director.session_context import DirectorRuntime
    from backend.main import create_app

    monkeypatch.setenv("APP_ENV", "prod")
    injected_deps.config = AppConfig(
        app_env="prod",
        render_backend="mock",
        cors_origins="https://console.example",
        director_enabled=True,
    )
    injected_deps.director = DirectorRuntime(
        injected_deps.backend,
        embedder=HashingEmbedder(),
    )
    app = create_app(config=injected_deps.config, deps=injected_deps)

    with TestClient(app) as client:
        body = client.get("/api/v1/health/ready").json()

    assert body["ok"] is False
    assert body["status"] == "not_ready"
    assert body["embedder"]["degraded"] is True


def test_health_ready_not_ready_when_llm_load_failed(mock_env: None, injected_deps) -> None:
    """Finding 2: a configured real LLM engine failed to load
    (em.llm_load_error set, em.llm is None) → /health/ready ok=False and the
    error is surfaced in the response."""
    from backend.main import create_app

    em: EngineManager = injected_deps.engine_manager
    # Simulate a failed real-engine load: configure a real engine name in the
    # cfg, leave llm=None, record the error.
    em._llm_cfg = {"engine": "llamacpp"}
    em.llm_load_error = "FileNotFoundError: GGUF file not found: /bad/path.gguf"
    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    body = r.json()
    # Mock backend → ready would be True, but llm_load_error → not ready.
    assert body["ok"] is False, (
        "Finding 2: /health/ready must report not-ready when a configured real "
        "LLM engine failed to load (even on mock backend)"
    )
    assert body["status"] == "not_ready"
    assert "llm_load_error" in body
    assert "GGUF file not found" in body["llm_load_error"]


def test_health_ready_not_ready_when_tts_load_failed(mock_env: None, injected_deps) -> None:
    """Finding 2: a configured real TTS engine failed to load
    (em.tts_load_error set, em.tts is None) → /health/ready ok=False."""
    from backend.main import create_app

    em: EngineManager = injected_deps.engine_manager
    em._tts_cfg = {"engine": "transformers"}
    em.tts_load_error = "ImportError: transformers not installed"
    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "not_ready"
    assert "tts_load_error" in body
    assert "transformers not installed" in body["tts_load_error"]


def test_health_ready_ready_when_no_error_and_stubs(mock_env: None, injected_deps) -> None:
    """Baseline: no load error, no engines loaded (stub defaults) → ready=True.
    Verifies the Finding 2 fix didn't break the normal stub path."""
    from backend.main import create_app

    app = create_app(deps=injected_deps)
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    body = r.json()
    assert body["ok"] is True
    assert "llm_load_error" not in body
    assert "tts_load_error" not in body


def test_engine_manager_load_llm_clears_error_on_success() -> None:
    """EngineManager.load_llm clears llm_load_error on a successful load
    (Finding 2). Verifies the clear-on-success contract."""
    from llm.engines.base import LLMEngine, LLMRequest, LLMResponse, register_engine  # noqa: F401

    @register_engine("mock-success-llm")
    class _MockSuccessLLM(LLMEngine):
        name = "mock-success-llm"

        def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
            return LLMResponse(text="ok", engine=self.name)

        @classmethod
        def from_config(cls, cfg: dict) -> "_MockSuccessLLM":
            return cls()

    em = EngineManager()
    em.llm_load_error = "prior failure"
    assert em.llm_failed is True
    em.load_llm({"engine": "mock-success-llm"})
    assert em.llm is not None
    assert em.llm_load_error is None, "load_llm must clear llm_load_error on success"
    assert em.llm_failed is False


def test_engine_manager_load_tts_clears_error_on_success() -> None:
    """EngineManager.load_tts clears tts_load_error on a successful load."""
    from tts.engines.base import AudioChunk, TTSRequest, TTSEngine, register_engine  # noqa: F401
    import numpy as np

    @register_engine("mock-success-tts")
    class _MockSuccessTTS(TTSEngine):
        name = "mock-success-tts"
        sample_rate = 24000

        def synthesize(self, req: TTSRequest) -> AudioChunk:  # pragma: no cover
            return AudioChunk(pcm=np.zeros(8, dtype=np.float32), sample_rate=self.sample_rate)

        @classmethod
        def from_config(cls, cfg: dict) -> "_MockSuccessTTS":
            e = cls()
            e.sample_rate = int(cfg.get("sample_rate", 24000))
            return e

    em = EngineManager()
    em.tts_load_error = "prior failure"
    assert em.tts_failed is True
    em.load_tts({"engine": "mock-success-tts", "sample_rate": 24000})
    assert em.tts is not None
    assert em.tts_load_error is None, "load_tts must clear tts_load_error on success"
    assert em.tts_failed is False


def test_create_app_records_llm_load_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 1 + 2 integration: create_app(env-driven, no deps) under mock
    mode with LLM_ENGINE set to a real engine that raises on from_config →
    the factory records the failure on engine_manager.llm_load_error AND
    /health/ready reports not-ready. The server still starts (stub fallback)."""
    from llm.engines.base import LLMEngine, LLMRequest, LLMResponse, register_engine  # noqa: F401

    @register_engine("mock-fail-llm")
    class _MockFailLLM(LLMEngine):
        name = "mock-fail-llm"

        @classmethod
        def from_config(cls, cfg: dict) -> "_MockFailLLM":
            raise FileNotFoundError("GGUF file not found: /bad/path.gguf")

        def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
            return LLMResponse(text="", engine=self.name)

    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "mock-fail-llm")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")

    from backend.main import create_app

    app = create_app(config=AppConfig.from_env())
    em = app.state.container.engine_manager
    assert em is not None
    # The load failed → llm is None (stub fallback), error is recorded.
    assert em.llm is None
    assert em.llm_load_error is not None
    assert "GGUF file not found" in em.llm_load_error
    assert em.llm_failed is True
    # /health/ready honestly reports not-ready.
    with TestClient(app) as client:
        r = client.get("/api/v1/health/ready")
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "not_ready"
    assert "llm_load_error" in body
    assert "GGUF file not found" in body["llm_load_error"]
