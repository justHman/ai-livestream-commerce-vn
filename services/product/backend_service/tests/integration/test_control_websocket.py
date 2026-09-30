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

from backend.config import AppConfig
from conftest import make_deps as _Deps  # noqa: F401


# NOTE: ``create_app`` is imported lazily inside ``_client`` so that
# ``backend.main`` (and its module-level ``CONFIG = AppConfig.from_env()``)
# is first imported while the ``mock_env`` fixture has already set
# ``RENDER_BACKEND=mock``. A module-level import here would cache ``CONFIG``
# with ``render_backend="cloud"`` during collection, before any fixture runs.


def _deps():
    return _Deps(
        director=None,
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


# ---------- P0-FB-016: legacy WS interrupt on a P0 session ----------


@pytest.mark.parametrize("p0", ["rescue", "p0_unmarked", "legacy"])
def test_ws_interrupt_on_p0_uses_execution_command(
    mock_env: None, monkeypatch: pytest.MonkeyPatch, p0: str
) -> None:
    monkeypatch.setenv("LIVENTO_P0_RESCUE_COMMANDS", "1")
    cfg = AppConfig(render_backend="mock", app_env="dev", backend_api_token="", debug_enabled=True)
    body = (
        {
            "execution_contract": "p0.execution.v1",
            "tenant_id": "tenant-1",
            "business_session_id": "business-1",
            "generation": "generation-1",
            "rescue_commands": p0 == "rescue",
        }
        if p0 != "legacy"
        else {}
    )
    with _client(cfg) as client:
        started = client.post("/api/v1/sessions", json=body)
        assert started.status_code == 200, started.text
        sid = started.json()["session_id"]
        with client.websocket_connect(f"/api/v1/ws/control/{sid}") as ws:
            assert ws.receive_json()["type"] == "control.connected"
            ws.send_json({"type": "interrupt"})
            ws.send_json({"type": "ping"})
            first = ws.receive_json()
    if p0 == "rescue":
        assert first["type"] == "error" and first["code"] == "use_execution_command"
    else:
        assert first["type"] != "error"
