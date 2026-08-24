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


def test_health_ready_stub_is_never_production_ready() -> None:
    # AVATAR_ENGINE=none builds the mock-model stub: it must never report a
    # production-ready self-host signal (audit R0.3) -> HTTP 503 test_stub_only.
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["reason"] == "test_stub_only"
    assert body["mode"] == "test_stub"


def test_health_ready_false_when_engine_unavailable() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.engine_is_stub = False
        app.state.engine_ready = False
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "engine_unavailable"


def test_health_ready_true_for_real_engine() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.engine_is_stub = False
        app.state.engine_ready = True
        resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["mode"] == "self_host"
