"""Integration: provider wiring through the app lifespan (Change T 6.7/6.8).

The real SDK is a system boundary; these tests drive the lifespan with a
monkeypatched ``vieneu.Vieneu`` factory so provider startup, readiness, and
the capabilities route are exercised end to end without loading a model.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tts import create_app

_PRESET_NAME = "Phạm Tuyên"


class FakeTTS:
    backend = "pytorch"

    def __init__(self) -> None:
        self.preset_voices = {
            _PRESET_NAME: {
                "speaker_emb": np.zeros(192, dtype=np.float32),
                "codes": np.zeros(62, dtype=np.int64),
            }
        }

    def get_preset_voice(self, name: str | None = None) -> dict:
        return self.preset_voices[_PRESET_NAME]

    def infer(self, text: str, **kwargs) -> np.ndarray:
        return np.zeros(48_000 // 10, dtype=np.float32)

    def _get_batch_engine(self):
        return None


@pytest.fixture
def provider_env(monkeypatch) -> FakeTTS:
    monkeypatch.setenv("TTS_PROVIDER", "vieneu_v3")
    monkeypatch.setenv("TTS_ACCELERATOR", "cpu")
    fake = FakeTTS()
    monkeypatch.setattr("vieneu.Vieneu", lambda mode, **kw: fake)
    return fake


def test_lifespan_starts_provider_and_readiness(provider_env) -> None:
    app = create_app()
    with TestClient(app) as client:
        assert app.state.provider is not None
        assert app.state.provider.backend == "pytorch"
        assert app.state.runtime_ready is True
        assert client.get("/ready").status_code == 200


def test_capabilities_route_reflects_active_provider(provider_env) -> None:
    app = create_app()
    with TestClient(app) as client:
        payload = client.get("/v1/audio/capabilities").json()
    assert payload["provider_name"] == "vieneu_v3"
    assert payload["sample_rate_hz"] == 48_000
    assert payload["supports_native_batch"] is True
    assert payload["max_batch_size"] == 32
    assert payload["supported_styles"] == ["natural", "news", "storytelling"]
    assert payload["supported_expressive_cues"] == ["[cười]", "[thở dài]", "[hắng giọng]"]
    assert "speaker_emb" not in __import__("json").dumps(payload)


def test_provider_init_failure_keeps_legacy_engine(monkeypatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER", "vieneu_v3")

    def boom(**kwargs):
        raise RuntimeError("model download failed")

    monkeypatch.setattr("vieneu.Vieneu", boom)
    app = create_app()
    with TestClient(app) as client:
        assert app.state.provider is None
        assert app.state.runtime_ready is False
        # Legacy engine still serves.
        assert client.post("/v1/speech", json={"text": "xin chào"}).status_code == 200
        # Readiness reflects the failed runtime.
        assert client.get("/ready").status_code == 503
        # Capabilities fall back to the config stub, not 503.
        caps = client.get("/v1/audio/capabilities").json()
        assert caps["provider_name"] == "vieneu_v3"
        assert caps["supports_native_batch"] is False
