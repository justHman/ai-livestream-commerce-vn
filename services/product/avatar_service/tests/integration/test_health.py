"""Integration: avatar health endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from avatar import create_app


def test_health_live() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_ready_true_for_real_engine_after_lifespan(monkeypatch) -> None:
    # A real self-host engine is ready after lifespan boots it.
    monkeypatch.setenv("AVATAR_ENGINE", "avatarforcing")
    monkeypatch.setenv("AVATAR_MODEL", "test-model")
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_health_ready_false_when_engine_unavailable() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.engine_ready = False
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"
