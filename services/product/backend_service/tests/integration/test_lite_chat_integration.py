"""Wave 2 integration test: /events -> DirectorCoordinator path.

TestClient flow:
  POST /api/v1/sessions         -> session_id
  POST /api/v1/sessions/{id}/attach -> coordinator started
  POST /api/v1/sessions/{id}/events x10 -> all 200, each accepted w/ comment_id
  coordinator.stats()           -> total comments observed
  POST /api/v1/sessions/{id}/stop -> coordinator dropped

Uses mock backend, stub LLM/TTS, hash embedder (all offline).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig

from backend.application.director.session_context import DirectorRuntime
from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator
from conftest import make_deps as _Deps  # noqa: F401


def _event(text: str, author: str, event_id: str, ts: float | None = None) -> dict:
    return {
        "event_id": event_id,
        "platform": "tiktok",
        "source_stream_id": "stream-1",
        "occurred_at": ts if ts is not None else time.time(),
        "type": "viewer.comment",
        "viewer": {"viewer_id": author, "display_name": author},
        "payload": {"text": text},
    }


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "1")
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")


def _make_app(mock_env) -> TestClient:
    """Build a dev-mode app with director + coordinator + event ingestion."""
    from backend.main import create_app
    from backend.application.director.embeddings import HashingEmbedder
    from backend.application.platform_events import PlatformEventIngestionService
    from llm.engines.base import _NoopEngine
    from tts.engines.base import ToneEngine

    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        director_enabled=True,
        debug_enabled=True,
    )
    deps = _Deps(config=cfg)
    backend = deps.backend
    runtime = DirectorRuntime(backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(
        runtime=runtime,
        llm=_NoopEngine(),
        tts=ToneEngine(),
        backend=backend,
        cfg=CoordinatorConfig(tick_ms=300, window_sec=75.0),
    )
    deps.director = runtime
    deps.coordinator = coordinator
    deps.event_ingestion = PlatformEventIngestionService(
        store=deps.store,
        coordinator=coordinator,
        runtime=runtime,
    )
    app = create_app(config=cfg, deps=deps)
    _make_app._coordinator = coordinator
    _make_app._runtime = runtime
    return TestClient(app)


def test_events_10_comments_accepted(mock_env: None) -> None:
    """start -> attach -> /events x10 -> stop; all accepted with unique ids."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]

        r = client.post(
            f"/api/v1/sessions/{sid}/attach",
            json={"products": []},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        comment_ids = []
        for i in range(10):
            r = client.post(
                f"/api/v1/sessions/{sid}/events",
                json={
                    "events": [
                        _event(
                            f"Comment #{i}: Gia bao nhieu?",
                            f"Viewer{i}",
                            f"evt-{i}",
                        )
                    ]
                },
            )
            assert r.status_code == 200, f"events #{i}: {r.status_code} {r.text}"
            body = r.json()
            assert body["accepted"] == 1
            assert body["duplicate"] == 0
            assert body["rejected"] == 0
            item = body["events"][0]
            assert item["status"] == "accepted"
            assert item["event_id"] == f"evt-{i}"
            assert "comment_id" in item
            comment_ids.append(item["comment_id"])

        assert len(set(comment_ids)) == 10

        r = client.post(f"/api/v1/sessions/{sid}/stop")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True


def test_debug_clusters_returns_canonical_cached_snapshot(mock_env: None) -> None:
    """Polling diagnostics uses Coordinator state and canonical cluster metrics."""
    with _make_app(mock_env) as client:
        response = client.post("/api/v1/sessions", json={"is_sandbox": True})
        session_id = response.json()["session_id"]
        response = client.post(
            f"/api/v1/sessions/{session_id}/attach",
            json={
                "products": [
                    {
                        "id": "P004",
                        "name": "Áo hoodie HeyGen",
                        "description": "Áo hoodie trắng",
                    }
                ],
            },
        )
        assert response.status_code == 200
        for index, text in enumerate(("Áo hoodie giá bao nhiêu?", "Hoodie này bao nhiêu tiền?")):
            response = client.post(
                f"/api/v1/sessions/{session_id}/events",
                json={
                    "events": [
                        _event(
                            text,
                            "viewer",
                            f"cluster-evt-{index}",
                        )
                    ]
                },
            )
            assert response.status_code == 200
            assert response.json()["accepted"] == 1

        coordinator = _make_app._coordinator
        assert coordinator is not None

        asyncio.run(coordinator._tick_once(session_id))
        first = coordinator.cluster_snapshot(session_id)
        second = coordinator.cluster_snapshot(session_id)

        assert first["received_total"] == 2
        assert first["buffered_comments"] == 2
        assert first["embedder_name"] == "hashing-fallback"
        from backend.application.director.config import StreamConfig

        assert first["cluster_merge_threshold"] == StreamConfig().cluster_merge_threshold
        assert first["clusters"] == second["clusters"]
        client.post(f"/api/v1/sessions/{session_id}/stop")


def test_events_without_attach_are_accepted_and_parked(mock_env: None) -> None:
    """POST /events on a started-but-unattached session parks comments on meta.

    The old sync Director fallback is gone (OpenSpec 2.12); with no
    coordinator the comment is accepted and parked (not lost), and later
    pickup is the coordinator's job.
    """
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        sid = r.json()["session_id"]

        r = client.post(
            f"/api/v1/sessions/{sid}/events",
            json={"events": [_event("hello", "test", "evt-noattach")]},
        )
        assert r.status_code == 200
        assert r.json()["accepted"] == 1
        assert "comment_id" not in r.json()["events"][0]  # parked, not queued

        client.post(f"/api/v1/sessions/{sid}/stop")


def test_events_text_too_long_rejected_413(mock_env: None) -> None:
    """Event text > 500 chars -> pydantic string_too_long -> 413 envelope."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        sid = r.json()["session_id"]
        client.post(
            f"/api/v1/sessions/{sid}/attach",
            json={"products": []},
        )

        r = client.post(
            f"/api/v1/sessions/{sid}/events",
            json={"events": [_event("x" * 501, "test", "evt-long")]},
        )
        assert r.status_code == 413

        client.post(f"/api/v1/sessions/{sid}/stop")


def test_events_unknown_session_returns_404(mock_env: None) -> None:
    """POST /events on a session that never existed -> 404."""
    with _make_app(mock_env) as client:
        r = client.post(
            "/api/v1/sessions/does-not-exist/events",
            json={"events": [_event("hello", "test", "evt-unknown")]},
        )
        assert r.status_code == 404


def test_lite_stop_drops_coordinator_session(mock_env: None) -> None:
    """After /stop, coordinator.has(sid) == False."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        sid = r.json()["session_id"]
        client.post(
            f"/api/v1/sessions/{sid}/attach",
            json={"products": []},
        )
        coordinator = _make_app._coordinator
        assert coordinator is not None
        assert coordinator.has(sid)

        client.post(f"/api/v1/sessions/{sid}/stop")

        assert not coordinator.has(sid)
