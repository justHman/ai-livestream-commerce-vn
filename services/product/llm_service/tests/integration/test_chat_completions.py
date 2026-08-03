"""Integration: chat completions, auth, limits, streaming, models."""

from __future__ import annotations

from fastapi.testclient import TestClient

from llm import create_app
from llm.config import SecurityConfig
from llm.engines.base import _NoopEngine


def _app(*, security: SecurityConfig | None = None):
    app = create_app(security=security)
    app.state.engine = _NoopEngine()
    app.state.engine_ready = True
    return app


def test_chat_completion_returns_text() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"].startswith("[noop]")


def test_chat_completion_streaming() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "data: [DONE]" in resp.text


def test_models_lists_active_engine() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    models = resp.json()["data"]
    assert models and models[0]["engine"] == "none"


def test_validation_error_bad_body() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json={"model": "m"})
    assert resp.status_code == 422


def test_auth_required_when_enabled() -> None:
    app = _app(
        security=SecurityConfig(auth_enabled=True, auth_token="secret")
    )
    with TestClient(app) as client:
        resp = client.get("/v1/models")
        assert resp.status_code == 401
        resp = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401
        resp = client.get("/v1/models", headers={"Authorization": "Bearer secret"})
        assert resp.status_code == 200


def test_body_limit_413() -> None:
    app = create_app()
    app.state.engine = _NoopEngine()
    with TestClient(app) as client:
        big = {"model": "m", "messages": [{"role": "user", "content": "x" * 200_000}]}
        resp = client.post("/v1/chat/completions", json=big)
    assert resp.status_code == 413