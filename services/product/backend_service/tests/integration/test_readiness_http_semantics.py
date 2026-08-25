"""Readiness HTTP-semantics truth table (audit R0.4).

Canonical readiness endpoints return HTTP 200 only when ready and HTTP 503
when a required dependency is unavailable. A JSON body with ``ok:false`` /
``status:not_ready`` must NEVER come back with HTTP 200. Liveness stays
process-only and 200 while the process lives.

Covers the canonical unversioned route, the v1 alias, and the admin deep-
health alias — all three share one implementation, so all three must be
truthful.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api.v1.hub import AvatarStore
from backend.application.db import InMemorySessionStore
from backend.application.render.mock import MockRenderBackend
from backend.engine_manager import EngineManager

from conftest import make_deps


def _app():
    from backend.main import create_app

    deps = make_deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=AvatarStore(),
        engine_manager=EngineManager(),
    )
    return create_app(deps=deps)


def test_ready_returns_200_when_ready_on_all_aliases(mock_env: None) -> None:
    app = _app()
    with TestClient(app) as client:
        for path in ("/health/ready", "/api/v1/health/ready", "/api/v1/admin/health"):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text}"
            assert resp.json()["status"] == "ready"


def test_ready_returns_503_when_llm_load_error(mock_env: None) -> None:
    app = _app()
    with TestClient(app) as client:
        em: EngineManager = app.state.container.engine_manager
        em.llm_load_error = "boom: engine boot failed"
        for path in ("/health/ready", "/api/v1/health/ready", "/api/v1/admin/health"):
            resp = client.get(path)
            assert resp.status_code == 503, f"{path}: {resp.status_code} {resp.text}"
            body = resp.json()
            assert body["ok"] is False
            assert body["status"] == "not_ready"
            assert "boom: engine boot failed" in body["llm_load_error"]


def test_ready_returns_503_when_container_missing(mock_env: None) -> None:
    app = _app()
    with TestClient(app) as client:
        app.state.container = None
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"


def test_liveness_stays_200_when_not_ready(mock_env: None) -> None:
    """Liveness is process-only: 200 even while readiness reports 503."""
    app = _app()
    with TestClient(app) as client:
        app.state.container.engine_manager.llm_load_error = "boom"
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "live"
