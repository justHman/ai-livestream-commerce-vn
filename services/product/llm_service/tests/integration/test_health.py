"""Integration: health endpoints and readiness truthfulness."""

from __future__ import annotations

from fastapi.testclient import TestClient

from llm import create_app


def test_health_live() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_ready_true_when_engine_started() -> None:
    app = create_app()
    with TestClient(app) as client:
        # lifespan builds the noop engine because LLM_ENGINE unset.
        resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_health_ready_false_when_engine_unavailable() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.engine_ready = False
        resp = client.get("/health/ready")
    assert resp.json()["status"] == "not_ready"