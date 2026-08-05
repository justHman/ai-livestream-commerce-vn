"""Offline tests for the canonical self-host TTS client.

The migrated ``test_tts_remote_engine.py`` (core/tests/test_tts_remote_engine.py)
tested the core ``remote_http`` engine adapter. In the service split the
tts_service deliberately REJECTS hosted adapters (``remote_http`` is not in
its ENGINES registry — see tts_service/tests/unit/test_engine_selection.py);
the remote TTS transport is owned by the backend control plane as
``backend.application.clients.tts.SelfHostedTTSClient``. These tests cover
that canonical client with httpx MockTransport — no real network.
"""

from __future__ import annotations

import io
import json
import wave

import httpx
import pytest

from backend.application.clients.tts.self_hosted import (
    SelfHostedTTSClient,
    TTSClientError,
)


def _pcm16_sine(n: int = 480, amp: float = 0.5) -> bytes:
    import numpy as np

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


def test_requires_base_url(monkeypatch):
    """No base_url and no env TTS_BASE_URL -> typed error naming base_url."""
    monkeypatch.delenv("TTS_BASE_URL", raising=False)
    with pytest.raises(TTSClientError, match="base_url"):
        SelfHostedTTSClient(base_url="")


def test_synthesize_pcm_body():
    raw = _pcm16_sine(480)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/speech")
        body = json.loads(request.content.decode("utf-8"))
        assert body["text"] == "Xin chào"
        return httpx.Response(
            200,
            content=raw,
            headers={
                "content-type": "application/octet-stream",
                "x-audio-sample-rate": "24000",
                "x-audio-duration-ms": "20",
                "x-audio-engine": "tone",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = SelfHostedTTSClient(base_url="http://tts:8002", http_client=client)

    result = engine.synthesize("Xin chào")

    assert result.pcm16 == raw
    assert result.sample_rate == 24_000
    assert result.duration_ms == 20
    assert result.engine == "tone"
    client.close()


def test_synthesize_wav_body_uses_header_rate():
    raw = _wav_bytes(_pcm16_sine(240), sample_rate=16_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "audio/wav"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = SelfHostedTTSClient(base_url="http://tts:8002/", http_client=client)

    result = engine.synthesize("hi")

    assert result.sample_rate == 24_000  # header default, not WAV header
    assert result.pcm16 == raw
    client.close()


def test_http_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = SelfHostedTTSClient(base_url="http://tts:8002", http_client=client)
    with pytest.raises(TTSClientError, match="HTTP 500"):
        engine.synthesize("x")
    client.close()
