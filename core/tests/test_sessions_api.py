"""Sessions aliases + admin + mock gate (M3). Offline mock backend."""

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
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")


def _deps() -> v1.V1Deps:
    return v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        director=None,
        engine_manager=EngineManager(),
        config=AppConfig(render_backend="mock", app_env="dev"),
    )


def _client(cfg: AppConfig | None = None) -> TestClient:
    from backend.main import create_app

    cfg = cfg or AppConfig(render_backend="mock", app_env="dev", debug_enabled=False)
    d = _deps()
    d.config = cfg
    app = create_app(config=cfg, deps=d)
    return TestClient(app)


def test_sessions_start_and_stop_alias(mock_env: None) -> None:
    with _client() as client:
        r = client.post("/api/v1/sessions", json={})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        # canonical say path
        r2 = client.post(f"/api/v1/sessions/{sid}/say", json={"text": "xin chào"})
        assert r2.status_code == 200, r2.text
        r3 = client.post(f"/api/v1/sessions/{sid}/say", json={"text": "deal hot"})
        assert r3.status_code == 200, r3.text
        r4 = client.post(f"/api/v1/sessions/{sid}/stop")
        assert r4.status_code == 200, r4.text
        assert r4.json()["stopped"] == sid


def test_plan_create_stores_run_plan(mock_env: None) -> None:
    with _client() as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        body = {
            "persona": "MC demo",
            "products": [
                {
                    "id": "p1",
                    "name": "Áo thun",
                    "features": ["vải cotton", "sale 50%"],
                    "price": 99000,
                }
            ],
        }
        r = client.post(f"/api/v1/sessions/{sid}/plan/create", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        plan = data["plan"]
        assert "opening" in plan["phases"]
        assert "selling" in plan["phases"]
        assert "closing" in plan["phases"]
        assert plan["selling"][0]["product_id"] == "p1"
        assert "vải cotton" in plan["selling"][0]["key_selling_points"]


def test_admin_config_no_secret_values(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="viewer-secret",
        admin_api_token="admin-secret",
    )
    admin = {"Authorization": "Bearer admin-secret"}
    with _client(cfg) as client:
        r = client.get("/api/v1/admin/config", headers=admin)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["secrets"]["backend_api_token"] == "present"
        assert "viewer-secret" not in r.text
        assert "admin-secret" not in r.text
        r2 = client.get("/api/v1/admin/health", headers=admin)
        assert r2.status_code == 200
        assert "status" in r2.json()


def test_mock_routes_404_in_prod_without_debug(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="prod",
        debug_enabled=False,
        cors_origins="https://example.com",
        backend_api_token="",
        admin_api_token="",
    )
    # create_app may reject prod CORS * — we set explicit origin.
    with _client(cfg) as client:
        r = client.get("/api/v1/mock/status/nope")
        assert r.status_code == 404
