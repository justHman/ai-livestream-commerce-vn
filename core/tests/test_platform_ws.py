"""WS /ws/platform/{session_id} (M3). Offline."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.engine_manager import EngineManager
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("APP_ENV", "dev")


def _client() -> TestClient:
    from core.server import create_app

    cfg = AppConfig(render_backend="mock", app_env="dev", backend_api_token="")
    deps = v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        engine_manager=EngineManager(),
        config=cfg,
    )
    return TestClient(create_app(config=cfg, deps=deps))


def test_platform_ws_stores_when_no_coordinator(mock_env: None) -> None:
    with _client() as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        with client.websocket_connect(f"/api/v1/ws/platform/{sid}") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "platform.connected"
            ws.send_json({"text": "giá bao nhiêu", "author": "u1"})
            ack = ws.receive_json()
            assert ack["type"] == "platform.stored"
            assert ack["pending"] >= 1


def test_platform_ws_auth_rejects_in_prod(mock_env: None) -> None:
    from core.server import create_app

    cfg = AppConfig(
        render_backend="mock",
        app_env="prod",
        backend_api_token="viewer-secret",
        admin_api_token="admin-secret",
        cors_origins="https://example.com",
    )
    deps = v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        engine_manager=EngineManager(),
        config=cfg,
    )
    with TestClient(create_app(config=cfg, deps=deps)) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/api/v1/ws/platform/s1"):
                pass
        with client.websocket_connect(
            "/api/v1/ws/platform/s1?token=viewer-secret"
        ) as ws:
            assert ws.receive_json()["type"] == "platform.connected"
