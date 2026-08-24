"""Readiness truthfulness: the stub (AVATAR_ENGINE=none) never reports ready.

R0.3/Decision 5: a stub container must not advertise production-ready
self-host. /health/ready returns 503 (not 200) until a real engine is
configured. Booting via create_app + TestClient (same as the integration
health tests) with the autouse offline_env fixture's dummy LiveKit env.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from avatar import create_app


def test_stub_engine_ready_returns_503() -> None:
    # offline_env fixture sets AVATAR_ENGINE=none -> stub -> not ready.
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "reason": "engine_unavailable"}


def test_lifespan_marks_stub_engine_not_ready() -> None:
    app = create_app()
    with TestClient(app) as client:
        assert client.app.state.engine_ready is False


def test_real_engine_ready_returns_200(monkeypatch) -> None:
    monkeypatch.setenv("AVATAR_ENGINE", "avatarforcing")
    monkeypatch.setenv("AVATAR_MODEL", "test-model")
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
