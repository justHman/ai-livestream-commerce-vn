"""Wave 2 integration test: /lite/chat -> DirectorCoordinator path.

TestClient flow:
  POST /lite/start       -> session_id
  POST /lite/attach      -> coordinator started
  POST /lite/chat x10    -> all 202, each has accepted + comment_id
  coordinator.stats()    -> total comments observed
  POST /lite/stop        -> coordinator dropped

Uses mock backend, stub LLM/TTS, hash embedder (all offline).
"""

from __future__ import annotations

import os

os.environ.setdefault("DIRECTOR_EMBEDDER", "hash")

import pytest
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig


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
    """Build a dev-mode app with director + coordinator wired."""
    from core.server import create_app

    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        director_enabled=True,
        debug_enabled=True,
    )
    # Build via env-driven path so the coordinator is constructed in server.py.
    app = create_app(config=cfg)
    return TestClient(app)


def test_lite_chat_10_comments_accepted(mock_env: None) -> None:
    """POST /lite/start -> /lite/attach -> /lite/chat x10 -> /lite/stop."""
    with _make_app(mock_env) as client:
        # Start session.
        r = client.post("/api/v1/lite/start", json={"is_sandbox": True})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]

        # Attach with empty product list (coordinator starts on attach).
        r = client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": sid,
                "products": [],
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        # POST /lite/chat 10 times.
        comment_ids = []
        for i in range(10):
            r = client.post(
                "/api/v1/lite/chat",
                json={
                    "session_id": sid,
                    "text": f"Comment #{i}: Gia bao nhieu?",
                    "author": f"Viewer{i}",
                },
            )
            assert r.status_code == 202, f"chat #{i}: {r.status_code} {r.text}"
            body = r.json()
            assert body["accepted"] is True
            assert "comment_id" in body
            comment_ids.append(body["comment_id"])
            # queue_stats should be present.
            assert "queue_stats" in body
            assert "queue" in body["queue_stats"]

        # All comment IDs should be unique.
        assert len(set(comment_ids)) == 10

        # Stop session.
        r = client.post("/api/v1/lite/stop", json={"session_id": sid})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True


def test_debug_clusters_returns_canonical_cached_snapshot(mock_env: None) -> None:
    """Polling diagnostics uses Coordinator state and canonical cluster metrics."""
    with _make_app(mock_env) as client:
        response = client.post("/api/v1/lite/start", json={"is_sandbox": True})
        session_id = response.json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": session_id,
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
        for text in ("Áo hoodie giá bao nhiêu?", "Hoodie này bao nhiêu tiền?"):
            response = client.post(
                "/api/v1/lite/chat",
                json={"session_id": session_id, "text": text, "author": "viewer"},
            )
            assert response.status_code == 202

        coordinator = v1.deps().coordinator
        assert coordinator is not None
        import asyncio

        asyncio.run(coordinator._tick_once(session_id))
        first = client.get(f"/api/v1/debug/clusters/{session_id}")
        second = client.get(f"/api/v1/debug/clusters/{session_id}")

        assert first.status_code == 200
        assert first.json()["received_total"] == 2
        assert first.json()["buffered_comments"] == 2
        assert first.json()["embedder_name"] == "hashing-fallback"
        from core.director.config import StreamConfig

        assert first.json()["cluster_merge_threshold"] == StreamConfig().cluster_merge_threshold
        assert first.json()["clusters"] == second.json()["clusters"]
        client.post("/api/v1/lite/stop", json={"session_id": session_id})


def test_lite_chat_without_attach_returns_404(mock_env: None) -> None:
    """POST /lite/chat on a session that was started but not attached -> 404."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/lite/start", json={"is_sandbox": True})
        sid = r.json()["session_id"]

        r = client.post(
            "/api/v1/lite/chat",
            json={
                "session_id": sid,
                "text": "hello",
                "author": "test",
            },
        )
        assert r.status_code == 404

        # Cleanup.
        client.post("/api/v1/lite/stop", json={"session_id": sid})


def test_lite_chat_text_too_long_returns_413(mock_env: None) -> None:
    """POST /lite/chat with text > 500 chars -> 413."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/lite/start", json={"is_sandbox": True})
        sid = r.json()["session_id"]
        client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": sid,
                "products": [],
            },
        )

        r = client.post(
            "/api/v1/lite/chat",
            json={
                "session_id": sid,
                "text": "x" * 501,
                "author": "test",
            },
        )
        assert r.status_code == 413

        client.post("/api/v1/lite/stop", json={"session_id": sid})


def test_lite_stop_drops_coordinator_session(mock_env: None) -> None:
    """After /lite/stop, coordinator.has(sid) == False."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/lite/start", json={"is_sandbox": True})
        sid = r.json()["session_id"]
        client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": sid,
                "products": [],
            },
        )
        # Coordinator should be active.
        d = v1.deps()
        assert d.coordinator is not None
        assert d.coordinator.has(sid)

        # Stop.
        client.post("/api/v1/lite/stop", json={"session_id": sid})

        # Coordinator should have dropped the session.
        assert not d.coordinator.has(sid)
