"""Integration: TTS synthesis and voices endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tts import create_app
from tts.config import SecurityConfig
from tts.engines.base import ToneEngine


def _app(*, security: SecurityConfig | None = None):
    app = create_app(security=security)
    app.state.engine = ToneEngine.from_config({})
    app.state.engine_ready = True
    return app


def test_speech_returns_pcm() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/v1/speech", json={"text": "Xin chào"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/pcm")
    assert int(resp.headers["x-audio-sample-rate"]) == 24_000
    assert len(resp.content) > 0


def test_speech_wav_format() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/v1/speech", json={"text": "Xin chào", "response_format": "wav"})
    assert resp.status_code == 200
    assert resp.content[:4] == b"RIFF"


def test_speech_validation_empty_text() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/v1/speech", json={"text": ""})
    assert resp.status_code == 422


def test_speech_validation_long_text() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/v1/speech", json={"text": "x" * 5000})
    assert resp.status_code == 422


def test_speech_bad_format_rejected() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/v1/speech", json={"text": "x", "response_format": "mp3"})
    assert resp.status_code == 422


def test_voices_reflect_active_engine() -> None:
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/v1/voices")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["engine"] == "tone"


def test_auth_required_when_enabled() -> None:
    app = _app(security=SecurityConfig(auth_enabled=True, auth_token="s3cr3t"))
    with TestClient(app) as client:
        assert client.get("/v1/voices").status_code == 401
        assert (
            client.get("/v1/voices", headers={"Authorization": "Bearer s3cr3t"}).status_code == 200
        )
