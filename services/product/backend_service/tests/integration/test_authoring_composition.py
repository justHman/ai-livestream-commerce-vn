"""Change B, B7: production composition wires ScriptAuthoringServiceImpl into the container.

With DATABASE_URL set, create_app() must inject a live authoring service so
POST /api/v1/script-sets returns 201 (not 501). Without DATABASE_URL the service
stays None and the surface keeps returning 501.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.config import AppConfig, TTSConfig


def _config(database_url: str | None = None) -> AppConfig:
    return AppConfig(
        app_env="dev",
        render_backend="mock",
        database_url=database_url or "",
        tts=TTSConfig(engine="tone"),  # stub — avoids offline transformers load
    )


async def test_script_sets_201_with_database_url(pg_url: str) -> None:
    from backend.main import create_app

    app = create_app(config=_config(pg_url))
    with TestClient(app) as client:
        container = app.state.container
        assert container.script_authoring_service is not None
        resp = client.post("/api/v1/script-sets", json={"name": "x", "product_ids": ["P1"]})
        assert resp.status_code == 201, resp.text
        set_id = resp.json()["id"]
        fetched = client.get(f"/api/v1/script-sets/{set_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["id"] == set_id


def test_script_sets_501_without_database_url() -> None:
    from backend.main import create_app

    app = create_app(config=_config(None))
    with TestClient(app) as client:
        assert app.state.container.script_authoring_service is None
        resp = client.post("/api/v1/script-sets", json={"name": "x", "product_ids": ["P1"]})
        assert resp.status_code == 501, resp.text
