"""End-to-end HTTP tests for POST /api/v1/sessions/{id}/events (OpenSpec 2.2)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from backend.application.platform_events import PlatformEventIngestionService
from backend.config import AppConfig

from conftest import make_deps as _Deps  # noqa: F401


def _event(event_id: str, text: str = "hello", viewer_id: str = "u1") -> dict:
    return {
        "event_id": event_id,
        "platform": "tiktok",
        "source_stream_id": "stream-1",
        "occurred_at": time.time(),
        "type": "viewer.comment",
        "viewer": {"viewer_id": viewer_id},
        "payload": {"text": text},
    }


def _client(
    *,
    app_env: str = "dev",
    backend_token: str = "",
    rate_limit: int = 30,
) -> TestClient:
    from backend.main import create_app

    config = AppConfig(
        render_backend="mock",
        app_env=app_env,
        backend_api_token=backend_token,
        admin_api_token=backend_token,
        cors_origins="https://example.com" if app_env == "prod" else "*",
        api_rate_limit_requests=rate_limit,
    )
    store = config.build_store()
    deps = _Deps(
        config=config,
        store=store,
        event_ingestion=PlatformEventIngestionService(store=store),
    )
    return TestClient(create_app(config=config, deps=deps))


def test_events_single_event_accepted() -> None:
    with _client() as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        r = client.post(f"/api/v1/sessions/{sid}/events", json={"events": [_event("e1")]})

    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1
    assert body["duplicate"] == 0
    assert body["rejected"] == 0
    assert body["events"][0]["status"] == "accepted"
    assert body["events"][0]["event_id"] == "e1"


def test_events_many_in_one_request() -> None:
    with _client() as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        r = client.post(
            f"/api/v1/sessions/{sid}/events",
            json={"events": [_event(f"m{i}", text=f"msg {i}") for i in range(10)]},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 10
    assert len(body["events"]) == 10


def test_events_duplicate_is_idempotent() -> None:
    with _client() as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        payload = {"events": [_event("dup")]}
        first = client.post(f"/api/v1/sessions/{sid}/events", json=payload)
        second = client.post(f"/api/v1/sessions/{sid}/events", json=payload)

    assert first.json()["accepted"] == 1
    body = second.json()
    assert body["duplicate"] == 1
    assert body["accepted"] == 0
    assert body["events"][0]["status"] == "duplicate"


def test_events_unknown_session_404() -> None:
    with _client() as client:
        r = client.post("/api/v1/sessions/nope/events", json={"events": [_event("x")]})

    assert r.status_code == 404


def test_events_malformed_422() -> None:
    with _client() as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        r = client.post(
            f"/api/v1/sessions/{sid}/events",
            json={"events": [{"event_id": "bad", "platform": "t"}]},
        )

    assert r.status_code == 422


def test_events_auth_required_in_prod() -> None:
    with _client(app_env="prod", backend_token="tok1234") as client:
        r = client.post("/api/v1/sessions/x/events", json={"events": [_event("a")]})
        ok = client.post(
            "/api/v1/sessions/x/events",
            json={"events": [_event("a")]},
            headers={"Authorization": "Bearer tok1234"},
        )

    assert r.status_code == 401
    assert ok.status_code == 404  # auth passed; session still unknown


def test_events_rate_limited_429() -> None:
    with _client(rate_limit=1) as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        client.post(f"/api/v1/sessions/{sid}/events", json={"events": [_event("r1")]})
        r = client.post(f"/api/v1/sessions/{sid}/events", json={"events": [_event("r2")]})

    assert r.status_code == 429


def test_events_rejected_reason_surfaces_in_response() -> None:
    with _client() as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        stale = _event("stale")
        stale["occurred_at"] = time.time() - 3600 * 24 * 2
        r = client.post(f"/api/v1/sessions/{sid}/events", json={"events": [stale]})

    assert r.status_code == 200
    body = r.json()
    assert body["rejected"] == 1
    assert body["events"][0]["reason"] == "occurred_at_out_of_range"
