"""Unit tests for WebSocket auth on /ws/control/{session_id} (Task 7).

Covers:
  - prod + valid viewer token via ?token=... -> connection accepted.
  - prod + no token -> connection rejected (closed before accept).
  - prod + wrong token -> connection rejected.
  - dev + no tokens set -> connection accepted (auth disabled).

All tests offline (mock backend).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.engine_manager import EngineManager
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore


# NOTE: ``create_app`` is imported lazily inside ``_client`` so that
# ``backend.main`` (and its module-level ``CONFIG = AppConfig.from_env()``)
# is first imported while the ``mock_env`` fixture has already set
# ``RENDER_BACKEND=mock``. A module-level import here would cache ``CONFIG``
# with ``render_backend="cloud"`` during collection, before any fixture runs.


def _deps() -> v1.V1Deps:
    return v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        director=None,
        engine_manager=EngineManager(),
    )


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")


def _client(cfg: AppConfig) -> TestClient:
    from backend.main import create_app

    app = create_app(config=cfg, deps=_deps())
    return TestClient(app)


def _prod_cfg() -> AppConfig:
    return AppConfig(
        render_backend="mock",
        app_env="prod",
        backend_api_token="viewer-secret",
        admin_api_token="admin-secret",
        debug_enabled=True,
        cors_origins="https://example.com",
    )


# ---------- prod: token required ----------


def test_prod_ws_valid_token_accepted(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        with client.websocket_connect("/api/v1/ws/control/sid-ok?token=viewer-secret") as ws:
            # First event is the control.connected handshake.
            hello = ws.receive_json()
            assert hello["type"] == "control.connected"
            ws.send_json({"type": "ping"})
            msg = ws.receive_json()
    assert msg["type"] == "pong"


def test_prod_ws_no_token_rejected(mock_env: None) -> None:
    """No token -> server closes before accept -> WebSocketDisconnect on enter."""
    with _client(_prod_cfg()) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/api/v1/ws/control/sid-none"):
                pass  # server should close before accept


def test_prod_ws_wrong_token_rejected(mock_env: None) -> None:
    with _client(_prod_cfg()) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/api/v1/ws/control/sid-wrong?token=nope"):
                pass  # server should close before accept


# ---------- dev: auth disabled ----------


def test_dev_ws_no_token_accepted(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        debug_enabled=True,
    )
    with _client(cfg) as client:
        with client.websocket_connect("/api/v1/ws/control/sid-dev") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "control.connected"
            ws.send_json({"type": "ping"})
            msg = ws.receive_json()
    assert msg["type"] == "pong"


# ---------- ws: control.connected event fires on successful connect ----------


def test_ws_connect_emits_control_connected(mock_env: None) -> None:
    """First event after accept should be control.connected."""
    with _client(_prod_cfg()) as client:
        with client.websocket_connect("/api/v1/ws/control/sid-conn?token=viewer-secret") as ws:
            msg = ws.receive_json()
    assert msg["type"] == "control.connected"
    assert msg["session_id"] == "sid-conn"
