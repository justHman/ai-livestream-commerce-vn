"""Offline tests for remote HTTP TTS client (Task 10).

Uses httpx MockTransport — no real network.
"""

from __future__ import annotations

import io
import json
import wave

import httpx
import numpy as np
import pytest

from core.tts.base import ENGINES, TTSRequest, load_engine


def _pcm16_sine(n: int = 480, amp: float = 0.5) -> bytes:
    t = np.arange(n, dtype=np.float32)
    pcm = (amp * np.sin(2 * np.pi * t / 40.0) * 32767.0).astype("<i2")
    return pcm.tobytes()


def _wav_bytes(pcm16: bytes, sample_rate: int = 24_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return buf.getvalue()


def test_remote_http_registered():
    import core.tts.adapters  # noqa: F401

    assert "remote_http" in ENGINES
    assert "remote" in ENGINES
    assert ENGINES["remote_http"] is ENGINES["remote"]


def test_from_config_requires_base_url(monkeypatch):
    monkeypatch.delenv("TTS_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="base_url"):
        load_engine({"engine": "remote_http", "sample_rate": 24000})


def test_synthesize_pcm_body():
    raw = _pcm16_sine(480)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/audio/speech")
        body = json.loads(request.content.decode("utf-8"))
        assert body["text"] == "Xin chào"
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/octet-stream"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = load_engine(
        {
            "engine": "remote_http",
            "base_url": "http://tts:8002",
            "sample_rate": 24_000,
            "http_client": client,
        }
    )
    chunk = engine.synthesize(TTSRequest(text="Xin chào"))
    assert chunk.sample_rate == 24_000
    assert chunk.pcm.dtype == np.float32
    assert chunk.pcm.shape[0] == 480
    assert float(np.max(np.abs(chunk.pcm))) > 0.1
    client.close()


def test_synthesize_wav_body():
    raw = _wav_bytes(_pcm16_sine(240), sample_rate=16_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "audio/wav"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = load_engine(
        {
            "engine": "remote",
            "base_url": "http://tts:8002/",
            "path": "/synthesize",
            "sample_rate": 24_000,
            "http_client": client,
        }
    )
    chunk = engine.synthesize(TTSRequest(text="hi"))
    assert chunk.sample_rate == 16_000  # from WAV header
    assert chunk.pcm.shape[0] == 240
    client.close()


def test_http_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = load_engine(
        {
            "engine": "remote_http",
            "base_url": "http://tts:8002",
            "http_client": client,
        }
    )
    with pytest.raises(RuntimeError, match="HTTP 500"):
        engine.synthesize(TTSRequest(text="x"))
    client.close()
