"""The old viewer-ingress contracts are not mounted (OpenSpec Decision 22).

- POST /api/v1/sessions/{id}/ingest      -> 404
- POST /api/v1/sessions/{id}/chat        -> 404
- WS   /api/v1/ws/platform/{id}          -> connect fails (not mounted)
- WS   /api/v1/ws/control/{id}           -> still works
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig

from conftest import make_deps as _Deps  # noqa: F401


def _client() -> TestClient:
    from backend.main import create_app

    cfg = AppConfig(render_backend="mock", app_env="dev", backend_api_token="")
    return TestClient(create_app(config=cfg, deps=_Deps(config=cfg)))


def test_ingest_route_not_mounted() -> None:
    with _client() as client:
        r = client.post("/api/v1/sessions/x/ingest", json={"comments": []})

    assert r.status_code == 404


def test_chat_route_not_mounted() -> None:
    with _client() as client:
        r = client.post("/api/v1/sessions/x/chat", json={"text": "hello", "author": "v"})

    assert r.status_code == 404


def test_platform_websocket_not_mounted() -> None:
    with _client() as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/api/v1/ws/platform/s1"):
                pass


def test_control_websocket_still_mounted() -> None:
    with _client() as client:
        with client.websocket_connect("/api/v1/ws/control/s1") as ws:
            hello = ws.receive_json()

    assert hello["type"] == "control.connected"
