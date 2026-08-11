"""Health/readiness/capabilities distinctions (Change T task 1.3).

/health is process liveness only; /ready reflects the active engine/provider
subsystem; /v1/audio/capabilities exposes provider-neutral facts with a
static stub until the runtime cluster wires the real provider.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tts import create_app
from tts.engines.base import ToneEngine

EXPECTED_CAPABILITY_FIELDS = frozenset(
    {
        "provider_name",
        "model_revision",
        "sample_rate_hz",
        "supports_native_batch",
        "max_batch_size",
        "supports_voice_cloning",
        "supports_mixed_voice_batch",
        "supported_styles",
        "supported_expressive_cues",
        "supported_response_formats",
    }
)


def test_capabilities_returns_provider_neutral_shape() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/audio/capabilities")
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload) == EXPECTED_CAPABILITY_FIELDS
    assert payload["provider_name"]
    assert payload["model_revision"]
    assert payload["sample_rate_hz"] > 0
    assert payload["max_batch_size"] >= 1
    assert payload["supported_response_formats"] == ["pcm", "wav"]


def test_capabilities_never_exposes_provider_payloads() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/audio/capabilities")
    raw = resp.text
    assert "speaker_emb" not in raw
    assert "ref_codes" not in raw


def test_capabilities_stub_matches_config_defaults() -> None:
    from tts.config import load_runtime_config

    cfg = load_runtime_config()
    app = create_app()
    with TestClient(app) as client:
        payload = client.get("/v1/audio/capabilities").json()
    assert payload["provider_name"] == cfg.provider
    assert payload["model_revision"] == cfg.model_revision


def test_health_returns_200_independent_of_engine() -> None:
    app = create_app()
    app.state.engine = None
    app.state.engine_ready = False
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_returns_200_when_engine_ready() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.engine = ToneEngine.from_config({})
        app.state.engine_ready = True
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_ready_returns_503_when_engine_not_ready() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.engine = None
        app.state.engine_ready = False
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "engine_unavailable"
    assert resp.json()["error"]["message"]
