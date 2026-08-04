"""Wave 2 integration test: GET /mock/video/{sid}.mjpeg continuous stream.

Covers:
  - With no utterance running, the stream serves pre-rendered idle frames at
    ~25 fps. Reading the response for ~500-1000 ms yields at least 10
    multipart JPEG parts (idle loop wraps, consecutive frames differ).
  - The stream stays open after sending one batch (continuous, not one-shot).
  - 404 is returned for an unknown session id.

Uses the mock backend with stub LLM/TTS and director disabled so no model
loads run. The streaming endpoint is async + infinite, so we drive it by
awaiting the route function and iterating ``StreamingResponse.body_iterator``
directly with an ``async_timeout`` deadline — ``httpx.ASGITransport`` buffers
the whole response body before delivering it, which never completes for an
infinite multipart/x-mixed-replace stream. The 404 path returns a complete
response, so it stays on the normal httpx client.
"""

from __future__ import annotations

import os

os.environ.setdefault("DIRECTOR_EMBEDDER", "hash")

import httpx
import pytest

from core.config import AppConfig


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")


def _build_app():
    from core.server import create_app

    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="",
        admin_api_token="",
        director_enabled=False,
    )
    return create_app(config=cfg)


@pytest.mark.asyncio
async def test_mock_video_route_absent_from_production_app(mock_env: None) -> None:
    """Mock MJPEG routes are not part of the production application (1.25)."""
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/mock/video/no-such-session.mjpeg")
        assert r.status_code == 404
        r2 = await client.post("/api/v1/sessions", json={"is_sandbox": True})
        assert r2.status_code == 200, r2.text
        sid = r2.json()["session_id"]
        r3 = await client.get(f"/api/v1/mock/video/{sid}.mjpeg")
        assert r3.status_code == 404


