"""Runtime resource discovery and TTS preview contracts."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.engine_manager import EngineManager
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore
from core.tts.base import AudioChunk, ToneEngine, TTSRequest


class _PreviewTone(ToneEngine):
    name = "preview-tone"

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        return AudioChunk(pcm=np.zeros(2400, dtype=np.float32), sample_rate=24000)


def _client() -> tuple[TestClient, EngineManager, v1.AvatarStore]:
    from backend.main import create_app

    config = AppConfig(
        render_backend="mock",
        app_env="dev",
        api_rate_limit_requests=100,
    )
    manager = EngineManager()
    manager._tts = _PreviewTone()
    manager._tts_cfg = {
        "engine": "tone",
        "model": "tone",
        "voice_id": "default",
        "sample_rate": 24000,
    }
    avatars = v1.AvatarStore()
    avatars.create(scope="half", ref_photo_url="https://example/avatar.jpg", voice="default")
    dependencies = v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        engine_manager=manager,
        config=config,
        avatars=avatars,
    )
    return TestClient(create_app(config=config, deps=dependencies)), manager, avatars


def test_discovery_returns_stable_safe_metadata() -> None:
    client, _, _ = _client()
    with client:
        engines = client.get("/api/v1/engines")
        avatars = client.get("/api/v1/avatars")

    assert engines.status_code == 200
    body = engines.json()
    assert all(
        {"id", "label", "engine", "model", "ready", "capabilities"} <= preset.keys()
        for preset in body["available_llm_presets"]
    )
    assert all(
        {"id", "label", "engine", "model", "ready", "capabilities"} <= preset.keys()
        for preset in body["available_tts_presets"]
    )
    assert body["tts"]["id"] == "tone:tone"
    assert body["voices"] == [{"id": "default", "label": "Default", "active": True}]
    assert "api_key" not in str(body).lower()

    avatar = avatars.json()["avatars"][0]
    assert {"id", "label", "ready", "capabilities", "thumbnail_url"} <= avatar.keys()


def test_tts_preview_returns_browser_playable_wav_and_metadata() -> None:
    client, _, _ = _client()
    with client:
        response = client.post(
            "/api/v1/engines/tts/preview",
            json={"text": "Xin chào", "tts_id": "tone:tone", "voice_id": "default"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content[:4] == b"RIFF"
    assert response.headers["x-tts-id"] == "tone:tone"
    assert response.headers["x-voice-id"] == "default"
    assert response.headers["x-sample-rate"] == "24000"


def test_tts_preview_rejects_inactive_known_preset_without_mislabeling_audio() -> None:
    client, manager, _ = _client()
    inactive = manager.status()["available_tts_presets"][0]["id"]
    with client:
        response = client.post(
            "/api/v1/engines/tts/preview",
            json={"text": "Xin chào", "tts_id": inactive, "voice_id": "default"},
        )

    assert response.status_code == 400


def test_failed_llm_swap_preserves_active_engine(monkeypatch) -> None:
    from core.llm.base import _NoopEngine

    manager = EngineManager()
    active = _NoopEngine()
    manager._llm = active
    manager._llm_cfg = {"engine": "none", "model": ""}

    def fail_load(cfg):
        raise RuntimeError("cannot load")

    monkeypatch.setattr("core.engine_manager.load_llm_engine", fail_load)

    with pytest.raises(RuntimeError, match="cannot load"):
        manager.load_llm({"engine": "vllm", "model": "missing"})

    assert manager.llm is active
    assert manager.llm_cfg == {"engine": "none", "model": ""}


def test_failed_tts_swap_preserves_active_engine(monkeypatch) -> None:
    manager = EngineManager()
    active = _PreviewTone()
    manager._tts = active
    manager._tts_cfg = {"engine": "tone", "model": "tone"}

    def fail_load(cfg):
        raise RuntimeError("cannot load")

    monkeypatch.setattr("core.engine_manager.load_tts_engine", fail_load)

    with pytest.raises(RuntimeError, match="cannot load"):
        manager.load_tts({"engine": "vieneu", "model": "missing"})

    assert manager.tts is active
    assert manager.tts_cfg == {"engine": "tone", "model": "tone"}


def test_tts_preview_rejects_unknown_resource_without_mutating_runtime() -> None:
    client, manager, _ = _client()
    before = dict(manager.tts_cfg)
    with client:
        response = client.post(
            "/api/v1/engines/tts/preview",
            json={"text": "Xin chào", "tts_id": "missing:model", "voice_id": "default"},
        )

    assert response.status_code == 400
    assert manager.tts_cfg == before
