"""Wave 2 integration test: GET /api/v1/engines + TTS preset selection.

Covers:
  - GET /api/v1/engines returns the 6 TTS preset entries from
    ``AVAILABLE_TTS_PRESETS`` under ``available_tts_presets``.
  - POST /api/v1/engines/tts/preset {preset_id} updates the engine manager's
    in-memory tts_cfg without loading the model and returns the new cfg.
  - Unknown preset_id -> 404.

Runs offline against a dev-mode app (auth disabled) with the mock backend
and stub LLM/TTS so no models are loaded.

Note: the spec calls for ``POST /api/v1/engines/tts {preset_id}``. The
existing ``POST /engines/tts`` route is reserved for full engine swaps
(``EngineSwapReq`` payload, loads model + reconfigures cloud backend); the
lightweight preset-by-id endpoint lives at ``/engines/tts/preset`` to avoid
the schema conflict. The frontend dropdown calls the preset endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig


EXPECTED_PRESET_IDS = {
    "vieneu-v3-turbo",
    "vieneu-v2",
    "cosyvoice2",
    "kokoro",
    "xtts-v2",
    "transformers-mms-vi",
}


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")  # not needed for engines tests
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")


def _make_app(mock_env) -> TestClient:
    from backend.main import create_app

    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        director_enabled=False,
    )
    app = create_app(config=cfg)
    return TestClient(app)


def test_engines_returns_six_tts_presets(mock_env: None) -> None:
    """GET /engines returns all 6 TTS presets with the expected ids."""
    with _make_app(mock_env) as client:
        r = client.get("/api/v1/engines")
        assert r.status_code == 200, r.text
        body = r.json()
        # The status payload exposes presets under ``available_tts_presets``.
        assert "available_tts_presets" in body
        presets = body["available_tts_presets"]
        assert isinstance(presets, list)
        assert len(presets) == 6
        ids = {p["id"] for p in presets}
        assert ids == EXPECTED_PRESET_IDS
        # Each entry must carry the dropdown-required fields.
        for p in presets:
            for key in ("id", "label", "engine", "weights_path", "sample_rate"):
                assert key in p, f"preset {p.get('id')} missing key {key}"


def test_engines_tts_preset_apply_vieneu_v2(mock_env: None) -> None:
    """POST /engines/tts/preset {vieneu-v2} -> 200, tts_cfg.sample_rate == 24000."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/engines/tts/preset", json={"preset_id": "vieneu-v2"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["preset_id"] == "vieneu-v2"
        cfg = body["tts_cfg"]
        assert cfg["engine"] == "vieneu"
        assert cfg["sample_rate"] == 24000
        assert cfg["weights_path"] == "pnnbao-ump/VieNeu-TTS-v2"


def test_engines_tts_preset_apply_v3_turbo(mock_env: None) -> None:
    """POST /engines/tts/preset {vieneu-v3-turbo} -> 48kHz sample rate."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/engines/tts/preset", json={"preset_id": "vieneu-v3-turbo"})
        assert r.status_code == 200, r.text
        body = r.json()
        cfg = body["tts_cfg"]
        assert cfg["sample_rate"] == 48000
        assert cfg["engine"] == "vieneu"


def test_engines_tts_preset_unknown_returns_404(mock_env: None) -> None:
    """POST /engines/tts/preset {bogus} -> 404."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/engines/tts/preset", json={"preset_id": "bogus"})
        assert r.status_code == 404
