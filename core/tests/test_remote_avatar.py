"""Offline tests for RemoteAvatarBackend (Task 14)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from core.config import AppConfig
from core.render.base import StartOptions
from core.render.remote_avatar import RemoteAvatarBackend
from core.render.windows import AudioWindow


def _handler_factory(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append((request.method, path, body))
        if path.endswith("/sessions/start"):
            return httpx.Response(
                200,
                json={
                    "session_id": "remote-1",
                    "livekit_url": "ws://lk",
                    "livekit_client_token": "tok",
                    "mode": "REMOTE",
                },
            )
        if "/audio" in path:
            return httpx.Response(
                200,
                json={
                    "video_windows": [
                        {
                            "seq": body.get("seq", 0),
                            "duration_ms": body.get("duration_ms", 0),
                            "is_final": body.get("is_final", False),
                            "fps": 25,
                            "frames": ["ZmFrZQ=="],
                        }
                    ]
                },
            )
        if path.endswith("/interrupt") or path.endswith("/stop"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text="not found")

    return handler


def test_remote_avatar_lifecycle():
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(_handler_factory(calls)))
    backend = RemoteAvatarBackend(
        base_url="http://avatar:8080",
        http_client=client,
    )
    assert backend.name == "remote_avatar"

    result = backend.start(StartOptions(avatar_id="a1", is_sandbox=True))
    assert result.session_id == "remote-1"
    assert result.livekit_url == "ws://lk"
    assert result.mode == "REMOTE"

    aw = AudioWindow(
        session_id="remote-1",
        utterance_id="u1",
        seq=0,
        sample_rate=24000,
        duration_ms=40,
        is_final=True,
        pcm=b"\x00\x01\x00\x02",
        text_span="hi",
    )
    windows = list(backend.stream_audio("remote-1", aw))
    assert len(windows) == 1
    assert windows[0].seq == 0
    assert windows[0].is_final is True
    assert windows[0].frames == ["ZmFrZQ=="]

    backend.interrupt("remote-1")
    backend.stop("remote-1")
    with pytest.raises(KeyError):
        backend.stop("remote-1")

    paths = [p for _, p, _ in calls]
    assert any(p.endswith("/sessions/start") for p in paths)
    assert any("/sessions/remote-1/audio" in p for p in paths)
    assert any(p.endswith("/interrupt") for p in paths)
    assert any(p.endswith("/stop") for p in paths)

    # audio body carried pcm_b64
    audio_call = next(c for c in calls if "/audio" in c[1])
    assert audio_call[2]["pcm_b64"] == base64.b64encode(b"\x00\x01\x00\x02").decode(
        "ascii"
    )
    client.close()


def test_requires_base_url(monkeypatch):
    monkeypatch.delenv("AVATAR_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="base_url"):
        RemoteAvatarBackend(base_url="")


def test_build_render_backend_remote_avatar(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "remote_avatar")
    monkeypatch.setenv("AVATAR_BASE_URL", "http://avatar:8080")
    cfg = AppConfig.from_env()
    backend = cfg.build_render_backend()
    assert isinstance(backend, RemoteAvatarBackend)
    assert backend.name == "remote_avatar"


def test_default_backends_unchanged(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("AVATAR_BASE_URL", raising=False)
    cfg = AppConfig.from_env()
    backend = cfg.build_render_backend()
    assert backend.name == "mock"


def test_stream_audio_unknown_session():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    backend = RemoteAvatarBackend(base_url="http://avatar:8080", http_client=client)
    aw = AudioWindow(
        session_id="nope",
        utterance_id="u",
        seq=0,
        sample_rate=16000,
        duration_ms=20,
        is_final=True,
    )
    with pytest.raises(KeyError):
        list(backend.stream_audio("nope", aw))
    client.close()


def test_http_error_on_start():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = RemoteAvatarBackend(base_url="http://avatar:8080", http_client=client)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        backend.start(StartOptions())
    client.close()
