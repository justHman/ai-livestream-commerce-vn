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

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig


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
    """Build a dev-mode app with director + coordinator injected."""
    from backend.main import create_app
    from backend.application.director.embeddings import HashingEmbedder
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
    backend = _Deps().backend
    runtime = DirectorRuntime(backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(
        runtime=runtime,
        llm=_NoopEngine(),
        tts=ToneEngine(),
        backend=backend,
        cfg=CoordinatorConfig(tick_ms=300, window_sec=75.0),
    )
    deps = _Deps(
        backend=backend,
        director=runtime,
        coordinator=coordinator,
        engine_manager=None,
        config=cfg,
    )
    app = create_app(config=cfg, deps=deps)
    _make_app._coordinator = coordinator
    _make_app._runtime = runtime
    return TestClient(app)


def test_lite_chat_10_comments_accepted(mock_env: None) -> None:
    """POST /lite/start -> /lite/attach -> /lite/chat x10 -> /lite/stop."""
    with _make_app(mock_env) as client:
        # Start session.
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]

        # Attach with empty product list (coordinator starts on attach).
        r = client.post(
            f"/api/v1/sessions/{sid}/attach",
            json={
                "products": [],
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        # POST /lite/chat 10 times.
        comment_ids = []
        for i in range(10):
            r = client.post(
                f"/api/v1/sessions/{sid}/chat",
                json={
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
        for text in ("Áo hoodie giá bao nhiêu?", "Hoodie này bao nhiêu tiền?"):
            response = client.post(
                f"/api/v1/sessions/{session_id}/chat",
                json={"text": text, "author": "viewer"},
            )
            assert response.status_code == 202

        coordinator = _make_app._coordinator
        assert coordinator is not None
        import asyncio

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


def test_lite_chat_without_attach_returns_404(mock_env: None) -> None:
    """POST /lite/chat on a session that was started but not attached -> 404."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        sid = r.json()["session_id"]

        r = client.post(
            f"/api/v1/sessions/{sid}/chat",
            json={
                "text": "hello",
                "author": "test",
            },
        )
        assert r.status_code == 404

        # Cleanup.
        client.post(f"/api/v1/sessions/{sid}/stop")


def test_lite_chat_text_too_long_returns_413(mock_env: None) -> None:
    """POST /lite/chat with text > 500 chars -> 413."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        sid = r.json()["session_id"]
        client.post(
            f"/api/v1/sessions/{sid}/attach",
            json={
                "products": [],
            },
        )

        r = client.post(
            f"/api/v1/sessions/{sid}/chat",
            json={
                "text": "x" * 501,
                "author": "test",
            },
        )
        assert r.status_code == 413

        client.post(f"/api/v1/sessions/{sid}/stop")


def test_lite_stop_drops_coordinator_session(mock_env: None) -> None:
    """After /lite/stop, coordinator.has(sid) == False."""
    with _make_app(mock_env) as client:
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        sid = r.json()["session_id"]
        client.post(
            f"/api/v1/sessions/{sid}/attach",
            json={
                "products": [],
            },
        )
        # Coordinator should be active.
        coordinator = _make_app._coordinator
        assert coordinator is not None
        assert coordinator.has(sid)

        # Stop.
        client.post(f"/api/v1/sessions/{sid}/stop")

        # Coordinator should have dropped the session.
        assert not coordinator.has(sid)


from backend.application.director.session_context import DirectorRuntime
from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator
from conftest import make_deps as _Deps  # noqa: F401
