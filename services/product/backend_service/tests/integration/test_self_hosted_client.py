"""Offline tests for SelfHostedAvatarClient (canonical outbound transport).

The legacy in-process ``RemoteAvatarBackend`` was replaced by the thin HTTP
client owned by the backend control plane (Task 1.22/1.32). Video/audio
streaming moved into the avatar_service media plane; the client exposes the
browser-safe start/stop lifecycle over the versioned avatar API.

Migrated from ``core/tests/test_remote_avatar.py`` (OpenSpec 1.50).
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.application.clients.avatar.self_hosted import (
    AvatarClientError,
    SelfHostedAvatarClient,
)


def _handler_factory(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append((request.method, path, body))
        if path.endswith("/v1/sessions"):
            return httpx.Response(
                200,
                json={
                    "session_id": "remote-1",
                    "livekit_url": "ws://lk",
                    "livekit_client_token": "tok",
                    "mode": "REMOTE",
                },
            )
        if path.endswith("/stop"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text="not found")

    return handler


def test_self_hosted_client_lifecycle():
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(_handler_factory(calls)))
    backend = SelfHostedAvatarClient(base_url="http://avatar:8080", http_client=client)

    result = backend.start(avatar_id="a1", is_sandbox=True)
    assert result.session_id == "remote-1"
    assert result.livekit_url == "ws://lk"
    assert result.livekit_client_token == "tok"
    assert result.mode == "REMOTE"

    backend.stop("remote-1")

    paths = [p for _, p, _ in calls]
    assert any(p.endswith("/v1/sessions") for p in paths)
    assert any(p.endswith("/v1/sessions/remote-1/stop") for p in paths)

    start_call = next(c for c in calls if c[1].endswith("/v1/sessions"))
    assert start_call[2] == {"avatar_id": "a1", "is_sandbox": True}
    client.close()


def test_requires_base_url(monkeypatch):
    monkeypatch.delenv("AVATAR_BASE_URL", raising=False)
    with pytest.raises(AvatarClientError, match="base_url"):
        SelfHostedAvatarClient(base_url="")


def test_http_error_on_start():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = SelfHostedAvatarClient(base_url="http://avatar:8080", http_client=client)
    with pytest.raises(AvatarClientError, match="HTTP 500"):
        backend.start()
    client.close()


def test_http_error_on_stop():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = SelfHostedAvatarClient(base_url="http://avatar:8080", http_client=client)
    with pytest.raises(AvatarClientError, match="HTTP 503"):
        backend.stop("remote-1")
    client.close()
