"""Offline tests for the canonical self-host TTS client.

The migrated ``test_tts_remote_engine.py`` (core/tests/test_tts_remote_engine.py)
tested the core ``remote_http`` engine adapter. In the service split the
tts_service deliberately REJECTS hosted adapters (``remote_http`` is not in
its ENGINES registry — see tts_service/tests/unit/test_engine_selection.py);
the remote TTS transport is owned by the backend control plane as
``backend.application.clients.tts.SelfHostedTTSClient``. These tests cover
that canonical client with httpx MockTransport — no real network.

Also covers the opt-in ``TTS_API_STYLE=openai_audio_speech`` wire contract
(VieNeu v3 Turbo GPU server: ``POST /v1/audio/speech``, raw PCM s16le, no
``x-audio-*`` headers) and asserts the default style is byte-for-byte unchanged.
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


_TTS_STYLE_VARS = (
    "TTS_API_STYLE",
    "TTS_MODEL_ID",
    "TTS_VOICE_ID",
    "TTS_SAMPLE_RATE",
    "TTS_AUTH_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_tts_style_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the wire-contract tests hermetic regardless of ambient env."""
    for var in _TTS_STYLE_VARS:
        monkeypatch.delenv(var, raising=False)


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


# ---------- OpenAI-style /v1/audio/speech (opt-in, TTS_API_STYLE) ----------


def _openai_client(
    handler,
    monkeypatch,
    base_url: str = "http://gpu:8080",
    *,
    api_key: str = "",
    **env,
) -> SelfHostedTTSClient:
    """Build a client pinned to the openai_audio_speech style."""
    monkeypatch.setenv("TTS_API_STYLE", "openai_audio_speech")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return SelfHostedTTSClient(
        base_url=base_url,
        api_key=api_key,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_openai_style_posts_to_v1_audio_speech_with_expected_body(monkeypatch):
    """Path, body fields and default 48 kHz sample rate for the GPU server."""
    raw = _pcm16_sine(48_000)  # 1.0 s at 48 kHz mono s16le
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, content=raw, headers={"content-type": "audio/pcm"})

    engine = _openai_client(handler, monkeypatch)
    result = engine.synthesize("Xin chào", voice="Hải Đăng")

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/audio/speech"
    assert seen["body"] == {
        "model": "vieneu-v3-turbo",
        "voice": "Hải Đăng",
        "input": "Xin chào",
        "response_format": "pcm",
        "stream_format": "audio",
        "sample_rate": 48_000,
    }

    # Raw PCM passthrough: no WAV header, rate echoed from the request.
    assert result.pcm16 == raw
    assert result.sample_rate == 48_000
    assert result.duration_ms == 1000  # len / (2 * 48000) * 1000
    assert result.engine == "vieneu-v3-turbo"
    engine.close()


def test_openai_style_sample_rate_follows_env(monkeypatch):
    raw = _pcm16_sine(16_000)  # 0.5 s at 32 kHz
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, content=raw)

    engine = _openai_client(handler, monkeypatch, TTS_SAMPLE_RATE="32000")
    result = engine.synthesize("hi", voice="Hải Đăng")

    assert seen["body"]["sample_rate"] == 32_000
    assert result.sample_rate == 32_000
    assert result.duration_ms == 500
    engine.close()


def test_openai_style_voice_falls_back_to_env_then_omitted(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, content=_pcm16_sine(96))

    # request voice wins over env
    engine = _openai_client(handler, monkeypatch, TTS_VOICE_ID="env-voice")
    engine.synthesize("hi", voice="request-voice")
    engine.close()
    assert bodies[0]["voice"] == "request-voice"

    # env voice used when the request carries none
    engine = _openai_client(handler, monkeypatch, TTS_VOICE_ID="env-voice")
    engine.synthesize("hi")
    engine.close()
    assert bodies[1]["voice"] == "env-voice"

    # neither -> field omitted entirely (server default voice)
    monkeypatch.delenv("TTS_VOICE_ID", raising=False)
    engine = _openai_client(handler, monkeypatch)
    engine.synthesize("hi")
    engine.close()
    assert "voice" not in bodies[2]


def test_openai_style_model_from_env(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, content=_pcm16_sine(96))

    engine = _openai_client(handler, monkeypatch, TTS_MODEL_ID="vieneu-v3-turbo-pro")
    result = engine.synthesize("hi", voice="Hải Đăng")
    engine.close()

    assert bodies[0]["model"] == "vieneu-v3-turbo-pro"
    assert result.engine == "vieneu-v3-turbo-pro"


def test_openai_style_sends_bearer_token(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=_pcm16_sine(96))

    # token from the constructor (compose cfg api_key)
    engine = _openai_client(handler, monkeypatch, TTS_AUTH_TOKEN="env-token", api_key="cfg-token")
    engine.synthesize("hi")
    engine.close()
    assert seen["auth"] == "Bearer cfg-token"

    # fallback to env TTS_AUTH_TOKEN
    engine = _openai_client(handler, monkeypatch, TTS_AUTH_TOKEN="env-token")
    engine.synthesize("hi")
    engine.close()
    assert seen["auth"] == "Bearer env-token"


def test_openai_style_error_status_raises_client_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model loading")

    engine = _openai_client(handler, monkeypatch)
    with pytest.raises(TTSClientError, match="HTTP 503"):
        engine.synthesize("hi", voice="Hải Đăng")
    engine.close()


def test_default_style_is_unchanged_when_api_style_unset(monkeypatch):
    """No TTS_API_STYLE (or an unknown value) keeps the /v1/speech contract."""
    for value in (None, "", "speech", "v1_speech"):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                content=_pcm16_sine(480),
                headers={"x-audio-sample-rate": "24000", "x-audio-engine": "tone"},
            )

        if value is None:
            monkeypatch.delenv("TTS_API_STYLE", raising=False)
        else:
            monkeypatch.setenv("TTS_API_STYLE", value)

        engine = SelfHostedTTSClient(
            base_url="http://tts:8002",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        result = engine.synthesize("Xin chào", voice="Hải Đăng")
        engine.close()

        assert seen["path"] == "/v1/speech"
        assert seen["body"] == {
            "text": "Xin chào",
            "voice": "Hải Đăng",
            "language": "vi",
            "response_format": "pcm",
        }
        assert result.sample_rate == 24_000
        assert result.engine == "tone"
