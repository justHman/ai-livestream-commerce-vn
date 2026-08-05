"""Integration: avatar discovery and session lifecycle."""

from __future__ import annotations

from fastapi.testclient import TestClient

from avatar import create_app
from avatar.config import SecurityConfig


def test_list_avatars() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/avatars")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["engine"] == "avatarforcing"


def test_create_and_lifecycle() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/v1/sessions", json={"avatar_id": "a1"})
        assert resp.status_code == 201
        body = resp.json()
        assert "livekit_url" in body
        assert "livekit_client_token" in body
        # No provider secret leaks into the response.
        serialized = str(body)
        assert "livekit_api_key" not in serialized
        assert "livekit_api_secret" not in serialized
        sid = body["session_id"]

        status = client.get(f"/v1/sessions/{sid}")
        assert status.status_code == 200
        assert status.json()["status"] == "active"

        assert client.post(f"/v1/sessions/{sid}/interrupt").status_code == 204
        assert client.post(f"/v1/sessions/{sid}/stop").status_code == 204


def test_missing_session_404() -> None:
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/v1/sessions/missing").status_code == 404
        assert client.post("/v1/sessions/missing/interrupt").status_code == 404


def test_auth_required_when_enabled() -> None:
    app = create_app(security=SecurityConfig(auth_enabled=True, auth_token="tok"))
    with TestClient(app) as client:
        assert client.get("/v1/avatars").status_code == 401
        assert client.get("/v1/avatars", headers={"Authorization": "Bearer tok"}).status_code == 200
