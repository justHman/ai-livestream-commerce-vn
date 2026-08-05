"""In-memory avatars CRUD (M3)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig
from conftest import make_deps as _Deps  # noqa: F401


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("APP_ENV", "dev")


def _client() -> TestClient:
    from backend.main import create_app

    cfg = AppConfig(render_backend="mock", app_env="dev")
    deps = _Deps(
        
        
        
        
        config=cfg,
    )
    return TestClient(create_app(config=cfg, deps=deps))


def test_avatars_crud_and_idle_stub(mock_env: None) -> None:
    with _client() as client:
        r = client.post(
            "/api/v1/avatars",
            json={"scope": "half", "ref_photo_url": "https://x/a.jpg", "voice": "v1"},
        )
        assert r.status_code == 200, r.text
        item = r.json()
        assert item["status"] == "ready"
        aid = item["avatar_id"]

        r_list = client.get("/api/v1/avatars")
        assert r_list.status_code == 200
        assert any(a["avatar_id"] == aid for a in r_list.json()["avatars"])

        r_get = client.get(f"/api/v1/avatars/{aid}")
        assert r_get.status_code == 200
        assert r_get.json()["scope"] == "half"

        r_put = client.put(f"/api/v1/avatars/{aid}", json={"scope": "full", "voice": "v2"})
        assert r_put.status_code == 200
        assert r_put.json()["scope"] == "full"
        assert r_put.json()["voice"] == "v2"

        r_idle = client.post(f"/api/v1/avatars/{aid}/idle/regenerate")
        assert r_idle.status_code == 200
        assert r_idle.json()["ok"] is True

        r_del = client.delete(f"/api/v1/avatars/{aid}")
        assert r_del.status_code == 200
        assert client.get(f"/api/v1/avatars/{aid}").status_code == 404
