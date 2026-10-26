"""POST /api/v1/tts/preview: same trust gate as GET /tts/voices, one preview at a time, only offered voices."""

from __future__ import annotations

import asyncio
import io
import wave

import pytest
from fastapi.testclient import TestClient

from backend.api.v1 import tts_voices
from backend.config import AppConfig, TTSConfig
from conftest import make_deps as _Deps

URL = "/api/v1/tts/preview"
VIEWER = {"Authorization": "Bearer viewer-test-token"}
AUTH = {**VIEWER, "X-Livento-Internal-Token": "internal-test-token"}
BODY = {"voice_id": "a", "text": "Xin chào mọi người."}


class FakeAudio:
    sample_rate = 24000

    def to_pcm16_bytes(self) -> bytes:
        return b"\x01\x00" * 240


class FakeEngine:
    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list = []
        self.error = error

    def synthesize(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return FakeAudio()


class FakeManager:
    def __init__(self, engine: FakeEngine | None) -> None:
        self.tts = engine


@pytest.fixture(autouse=True)
def _offered_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setattr(tts_voices, "_cached_voices", lambda base_url, api_key: [{"id": "a"}])
    monkeypatch.setattr(tts_voices, "_preview_slot", asyncio.Semaphore(1))


def _client(engine: FakeEngine | None, tts_engine: str = "remote_http") -> TestClient:
    from backend.main import create_app

    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="viewer-test-token",
        avatar_internal_token="internal-test-token",
        tts=TTSConfig(engine=tts_engine, base_url="http://tts.test"),
    )
    deps = _Deps(config=cfg, engine_manager=FakeManager(engine))
    return TestClient(create_app(config=cfg, deps=deps))


def test_returns_a_playable_wav_of_the_requested_voice() -> None:
    engine = FakeEngine()
    with _client(engine) as client:
        r = client.post(URL, json=BODY, headers=AUTH)
    with wave.open(io.BytesIO(r.content)) as wav:
        rate = wav.getframerate()
    assert (r.status_code, r.headers["content-type"], rate) == (200, "audio/wav", 24000)


def test_the_engine_gets_the_voice_and_text() -> None:
    engine = FakeEngine()
    with _client(engine) as client:
        client.post(URL, json=BODY, headers=AUTH)
    assert [(q.voice, q.text) for q in engine.requests] == [("a", "Xin chào mọi người.")]


def test_viewer_token_alone_is_forbidden_and_never_reaches_the_engine() -> None:
    engine = FakeEngine()
    with _client(engine) as client:
        r = client.post(URL, json=BODY, headers=VIEWER)
    assert (r.status_code, engine.requests) == (403, [])


def test_a_voice_that_is_not_offered_is_rejected() -> None:
    engine = FakeEngine()
    with _client(engine) as client:
        r = client.post(URL, json={**BODY, "voice_id": "other"}, headers=AUTH)
    assert (r.status_code, r.json()["error"]["code"], engine.requests) == (400, "unknown_voice", [])


def test_a_non_remote_engine_is_unavailable() -> None:
    with _client(FakeEngine(), tts_engine="tone") as client:
        r = client.post(URL, json=BODY, headers=AUTH)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "tts_unavailable")


def test_text_over_300_characters_is_rejected() -> None:
    engine = FakeEngine()
    with _client(engine) as client:
        r = client.post(URL, json={**BODY, "text": "a" * 301}, headers=AUTH)
    assert (r.status_code, engine.requests) == (413, [])


def test_a_second_preview_while_one_runs_is_turned_away(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hold() -> asyncio.Semaphore:
        slot = asyncio.Semaphore(1)
        await slot.acquire()
        return slot

    monkeypatch.setattr(tts_voices, "_preview_slot", asyncio.run(hold()))
    engine = FakeEngine()
    with _client(engine) as client:
        r = client.post(URL, json=BODY, headers=AUTH)
    assert (r.status_code, r.json()["error"]["code"], engine.requests) == (429, "preview_busy", [])


def test_an_engine_failure_is_a_502_without_the_text_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = FakeEngine(error=RuntimeError("boom"))
    with _client(engine) as client:
        r = client.post(URL, json=BODY, headers=AUTH)
    assert (r.status_code, r.json()["error"]["code"], "mọi người" in caplog.text) == (
        502,
        "preview_failed",
        False,
    )
