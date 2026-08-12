"""Integration: response tracing headers and batch-route absence (Change T 3.3/3.7/3.8).

Tracing metadata travels in response headers (X-Request-Id, X-Session-Id,
X-Utterance-Id, X-Chunk-Seq) — never raw text, embeddings, or ref codes.
The public API intentionally has no /v1/audio/speech/batch route; the
scheduler that consumes these headers lands in the runtime cluster.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tts import create_app
from tts.engines.base import ToneEngine


def _app() -> TestClient:
    app = create_app()
    app.state.engine = ToneEngine.from_config({})
    app.state.engine_ready = True
    return TestClient(app)


def test_tracing_headers_echo_request_metadata() -> None:
    with _app() as client:
        resp = client.post(
            "/v1/speech",
            json={
                "text": "Xin chào",
                "session_id": "sess-abc",
                "utterance_id": "utt-42",
                "chunk_seq": 7,
            },
        )
    assert resp.status_code == 200
    assert resp.headers["x-request-id"]
    assert resp.headers["x-session-id"] == "sess-abc"
    assert resp.headers["x-utterance-id"] == "utt-42"
    assert resp.headers["x-chunk-seq"] == "7"


def test_tracing_headers_generated_when_metadata_missing() -> None:
    with _app() as client:
        resp = client.post("/v1/speech", json={"text": "Xin chào"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"]
    assert resp.headers["x-session-id"] == "anonymous"
    assert resp.headers["x-utterance-id"] == "anonymous"
    assert resp.headers["x-chunk-seq"] == "0"


def test_alias_path_echoes_tracing_headers() -> None:
    with _app() as client:
        resp = client.post(
            "/v1/audio/speech",
            json={"text": "Xin chào", "session_id": "s1", "utterance_id": "u1"},
        )
    assert resp.status_code == 200
    assert resp.headers["x-session-id"] == "s1"
    assert resp.headers["x-utterance-id"] == "u1"


def test_tracing_headers_never_carry_raw_text() -> None:
    with _app() as client:
        resp = client.post("/v1/speech", json={"text": "nội dung bí mật"})
    raw = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
    assert "nội dung bí mật" not in raw


def test_no_public_batch_route() -> None:
    with _app() as client:
        post = client.post("/v1/audio/speech/batch", json={"text": "x"})
        get = client.get("/v1/audio/speech/batch")
    assert post.status_code == 404
    assert get.status_code == 404
