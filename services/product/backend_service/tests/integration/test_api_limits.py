"""Rate, body-size, and input boundaries for the public API."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.application.render.limiters import MaxBodySizeMiddleware, SlidingWindowLimiter
from backend.config import AppConfig
from conftest import make_deps as _Deps  # noqa: F401


def _client(config: AppConfig) -> TestClient:
    from backend.main import create_app

    deps = _Deps(
        config=config,
    )
    return TestClient(create_app(config=config, deps=deps))


def test_limiter_allows_exact_limit() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10, max_keys=2)

    assert limiter.allow("viewer", now=100.0)
    assert limiter.allow("viewer", now=100.0)


def test_limiter_rejects_request_after_exact_limit() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=10, max_keys=2)
    limiter.allow("viewer", now=100.0)

    assert not limiter.allow("viewer", now=100.0)


def test_limiter_expires_events_at_exact_window_boundary() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=10, max_keys=2)
    limiter.allow("viewer", now=100.0)

    assert limiter.allow("viewer", now=110.0)


def test_limiter_evicts_stale_key_before_active_key() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=10, max_keys=2)
    limiter.allow("stale", now=0.0)
    limiter.allow("active", now=5.0)
    limiter.allow("new", now=11.0)

    assert not limiter.allow("active", now=11.0)


def test_limiter_evicts_oldest_active_key_when_full() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, max_keys=2)
    limiter.allow("oldest", now=0.0)
    limiter.allow("newer", now=1.0)
    limiter.allow("replacement", now=2.0)

    assert limiter.allow("oldest", now=2.0)


def test_limiter_bounds_rotating_keys() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=1_000_000, max_keys=2)
    for index in range(10_000):
        limiter.allow(f"session-{index}", now=float(index))

    assert limiter.allow("session-0", now=10_000.0)


def test_limiter_is_thread_safe_for_one_key() -> None:
    limit = 8
    workers = 32
    limiter = SlidingWindowLimiter(limit=limit, window_seconds=60, max_keys=1)
    barrier = threading.Barrier(workers)

    def request(_: None) -> bool:
        barrier.wait()
        return limiter.allow("viewer", now=100.0)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        allowed = list(executor.map(request, [None] * workers))

    assert sum(allowed) == limit


@pytest.mark.asyncio
async def test_chunked_body_over_limit_returns_413() -> None:
    received = iter(
        (
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"678901", "more_body": False},
        )
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return next(received)

    async def send(message: dict) -> None:
        sent.append(message)

    async def app(scope: dict, receive, send) -> None:
        while (await receive()).get("more_body", False):
            pass

    middleware = MaxBodySizeMiddleware(app, max_bytes=10)
    await middleware({"type": "http", "headers": []}, receive, send)

    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_exact_body_limit_reaches_app() -> None:
    received = iter(({"type": "http.request", "body": b"1234567890", "more_body": False},))
    received_body: bytes | None = None

    async def receive() -> dict:
        return next(received)

    async def send(message: dict) -> None:
        return None

    async def app(scope: dict, receive, send) -> None:
        nonlocal received_body
        received_body = (await receive())["body"]

    middleware = MaxBodySizeMiddleware(app, max_bytes=10)
    await middleware({"type": "http", "headers": []}, receive, send)

    assert received_body == b"1234567890"


def test_scoped_rest_limit_returns_429() -> None:
    config = AppConfig(
        render_backend="mock",
        app_env="dev",
        api_rate_limit_requests=1,
        api_rate_limit_window_seconds=60,
    )
    with _client(config) as client:
        client.post("/api/v1/sessions", json={})
        response = client.post("/api/v1/sessions", json={})

    assert response.status_code == 429


def test_health_is_unrestricted() -> None:
    config = AppConfig(
        render_backend="mock",
        app_env="dev",
        api_rate_limit_requests=1,
        api_rate_limit_window_seconds=60,
    )
    with _client(config) as client:
        client.post("/api/v1/sessions", json={})
        client.post("/api/v1/sessions", json={})
        response = client.get("/api/v1/health")

    assert response.status_code == 200


def test_admin_mutation_is_rate_limited() -> None:
    config = AppConfig(
        render_backend="mock",
        app_env="dev",
        api_rate_limit_requests=1,
        api_rate_limit_window_seconds=60,
    )
    with _client(config) as client:
        client.post("/api/v1/engines/tts/preset", json={"preset_id": "vieneu-v2"})
        response = client.post("/api/v1/engines/tts/preset", json={"preset_id": "vieneu-v2"})

    assert response.status_code == 429


def test_exact_vietnamese_emoji_boundaries_are_accepted() -> None:
    from backend.api.v1.router import AvatarCreateReq, PlanCreateReq, SayReq
    from backend.application.platform_events import EventsIn, PlatformEvent, ViewerRef

    say = SayReq(session_id="session", text="🛍" * 2_000)
    plan = PlanCreateReq(persona="ệ" * 1_000)
    avatar = AvatarCreateReq(ref_photo_url="u" * 2_048)
    event = PlatformEvent(
        event_id="ế" * 128,
        platform="t" * 32,
        source_stream_id="s" * 256,
        occurred_at=1_700_000_000.0,
        type="viewer.comment",
        viewer=ViewerRef(viewer_id="👤" * 128, display_name="ế" * 128),
        payload={"text": "ế" * 500},
    )
    batch = EventsIn(events=[event])

    assert (
        len(say.text),
        len(event.event_id),
        len(event.platform),
        len(event.source_stream_id),
        len(event.viewer.viewer_id),
        len(event.viewer.display_name),
        len(event.payload.text),
        len(batch.events),
        len(plan.persona),
        len(avatar.ref_photo_url),
    ) == (
        2_000,
        128,
        32,
        256,
        128,
        128,
        500,
        1,
        1_000,
        2_048,
    )


def test_viewer_id_max_plus_one_is_rejected() -> None:
    from backend.application.platform_events import PlatformEvent, ViewerRef

    with pytest.raises(ValidationError):
        PlatformEvent(
            event_id="e1",
            platform="t",
            source_stream_id="s",
            occurred_at=1_700_000_000.0,
            type="viewer.comment",
            viewer=ViewerRef(viewer_id="x" * 129),
            payload={"text": "text"},
        )


def test_persona_max_plus_one_is_rejected() -> None:
    from backend.api.v1.router import PlanCreateReq

    with pytest.raises(ValidationError):
        PlanCreateReq(persona="x" * 1_001)


def test_url_max_plus_one_is_rejected() -> None:
    from backend.api.v1.router import AvatarCreateReq

    with pytest.raises(ValidationError):
        AvatarCreateReq(ref_photo_url="x" * 2_049)


def test_say_max_plus_one_is_rejected() -> None:
    from backend.api.v1.router import SayReq

    with pytest.raises(ValidationError):
        SayReq(session_id="session", text="x" * 2_001)


def test_event_batch_over_limit_is_rejected() -> None:
    from backend.application.platform_events import EventsIn

    event = {
        "event_id": "e",
        "platform": "t",
        "source_stream_id": "s",
        "occurred_at": 1_700_000_000.0,
        "type": "viewer.comment",
        "payload": {"text": "x"},
    }
    with pytest.raises(ValidationError):
        EventsIn(events=[event] * 101)


def test_event_text_over_limit_is_rejected() -> None:
    from backend.application.platform_events import PlatformEvent

    with pytest.raises(ValidationError):
        PlatformEvent(
            event_id="e1",
            platform="t",
            source_stream_id="s",
            occurred_at=1_700_000_000.0,
            type="viewer.comment",
            payload={"text": "x" * 501},
        )


def test_validation_413_does_not_echo_submitted_marker() -> None:
    config = AppConfig(render_backend="mock", app_env="dev")
    marker = "sensitive-marker-should-not-echo"
    with _client(config) as client:
        response = client.post(
            f"/api/v1/sessions/{'session'}/events",
            json={
                "events": [
                    {
                        "event_id": "e1",
                        "platform": "t",
                        "source_stream_id": "s",
                        "occurred_at": 1.0,
                        "type": "viewer.comment",
                        "payload": {"text": marker * 20},
                    }
                ]
            },
        )

    assert (response.status_code, marker not in response.text) == (413, True)


def test_oversized_body_keeps_cors_header() -> None:
    config = AppConfig(
        render_backend="mock",
        app_env="dev",
        cors_origins="https://frontend.example",
        max_request_body_bytes=10,
    )
    with _client(config) as client:
        response = client.post(
            "/api/v1/sessions",
            content="x" * 11,
            headers={"origin": "https://frontend.example", "content-type": "application/json"},
        )

    assert (response.status_code, response.headers["access-control-allow-origin"]) == (
        413,
        "https://frontend.example",
    )


def test_control_websocket_burst_closes_with_policy_violation() -> None:
    config = AppConfig(
        render_backend="mock",
        app_env="dev",
        ws_rate_limit_messages=1,
        ws_rate_limit_window_seconds=60,
    )
    with _client(config) as client:
        with client.websocket_connect("/api/v1/ws/control/session") as ws:
            ws.receive_json()
            ws.send_json({"type": "ping"})
            ws.receive_json()
            ws.send_json({"type": "ping"})
            with pytest.raises(Exception) as error:
                ws.receive_json()

    assert getattr(error.value, "code", None) == 1008
