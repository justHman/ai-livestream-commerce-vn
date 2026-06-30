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

import asyncio
import os

os.environ.setdefault("DIRECTOR_EMBEDDER", "hash")

import async_timeout
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


def _split_mjpeg_parts(raw: bytes) -> list[bytes]:
    """Split a multipart/x-mixed-replace payload into JPEG bodies."""
    parts: list[bytes] = []
    boundary = b"--mockmjpegboundary"
    for piece in raw.split(boundary):
        idx = piece.find(b"\r\n\r\n")
        if idx == -1:
            continue
        header = piece[:idx]
        body = piece[idx + 4 :]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        if b"image/jpeg" not in header:
            continue
        cl_idx = header.find(b"Content-Length:")
        if cl_idx == -1:
            continue
        cl_end = header.find(b"\r\n", cl_idx)
        try:
            length = int(header[cl_idx + len(b"Content-Length:") : cl_end].strip())
        except ValueError:
            continue
        if len(body) >= length:
            parts.append(bytes(body[:length]))
    return parts


async def _drive_mjpeg(app, sid: str, *, max_bytes: int, deadline_s: float) -> bytes:
    """Read up to ``max_bytes`` from the MJPEG stream or stop at deadline.

    Drives the route's ``StreamingResponse.body_iterator`` directly instead of
    going through ``httpx.ASGITransport``. The ASGI transport buffers the full
    response body before yielding any bytes to ``aiter_*``; for an infinite
    multipart/x-mixed-replace stream that means the deadline fires with zero
    bytes received. Iterating the body iterator under an ``async_timeout``
    deadline returns the frames as the endpoint yields them.
    """
    from core.api.v1 import mock_video_mjpeg

    resp = await mock_video_mjpeg(sid)
    assert resp.status_code == 200
    assert "multipart/x-mixed-replace" in resp.media_type
    buf = bytearray()
    try:
        # async_timeout works on Python 3.10+; asyncio.timeout is 3.11-only.
        async with async_timeout.timeout(deadline_s):
            async for chunk in resp.body_iterator:
                if isinstance(chunk, (bytes, bytearray)):
                    buf.extend(chunk)
                if len(buf) >= max_bytes:
                    break
    except asyncio.TimeoutError:
        pass
    return bytes(buf)


@pytest.mark.asyncio
async def test_mjpeg_continuous_idle_serves_many_frames(mock_env: None) -> None:
    """No utterance running -> idle stream yields >=10 multipart JPEG parts."""
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/v1/lite/start", json={"is_sandbox": True})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]

    raw = await _drive_mjpeg(app, sid, max_bytes=200_000, deadline_s=2.5)

    parts = _split_mjpeg_parts(raw)
    assert len(parts) >= 10, (
        f"expected >=10 multipart JPEG frames, got {len(parts)} "
        f"(total bytes {len(raw)})"
    )
    # Idle loop is 75 frames @ 25fps so within the first ~10 parts at least
    # two of them must differ (frame N != frame N+1).
    head = parts[:10]
    assert len(set(head)) >= 2, "expected at least 2 distinct idle frames"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v1/lite/stop", json={"session_id": sid})


@pytest.mark.asyncio
async def test_mjpeg_unknown_session_returns_404(mock_env: None) -> None:
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/mock/video/no-such-session.mjpeg")
        assert r.status_code == 404
