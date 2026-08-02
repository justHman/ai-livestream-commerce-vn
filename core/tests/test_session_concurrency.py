"""Unit tests for per-session concurrency + interrupt cancel (Task 8).

Covers:
  - Two CONCURRENT /lite/say on the same session (mock backend, stub LLM+TTS):
    one returns 200, the other 409 already_speaking.
  - After the 200 completes, a new say on the same session succeeds (lock
    released).
  - /lite/interrupt during a long say: the say completes (cancelled) and
    interrupt returns 200.
  - Cloud backend /lite/say still calls backend.say (streaming path NOT taken
    for cloud) — smoke test that cloud path is unchanged.

Uses ``httpx.AsyncClient`` with the ASGI transport for true concurrency
(two simultaneous POSTs via asyncio.gather). All tests offline.
"""

from __future__ import annotations

import asyncio
import time
from typing import Iterator

import httpx
import pytest
from httpx import ASGITransport

from core.api import v1
from core.config import AppConfig
from core.engine_manager import EngineManager
from core.llm.base import LLMEngine, LLMRequest, LLMResponse
from core.render.base import FullPipelineBackend, RenderBackend, StartOptions, StartResult
from core.render.mock import MockRenderBackend
from core.render.windows import AudioWindow, TextChunk
from core.store import InMemorySessionStore
from core.tts.base import AudioChunk, TTSEngine, TTSRequest


pytestmark = pytest.mark.asyncio


# ---------- stubs ----------


class _SlowStubLLM(LLMEngine):
    """LLM stub that yields many TextChunks with a tiny sleep between each so a
    run takes long enough to be interruptible."""

    name = "slow-stub-llm"

    def __init__(self, n_chunks: int = 20, delay_s: float = 0.02) -> None:
        self._n = n_chunks
        self._delay = delay_s

    @classmethod
    def from_config(cls, cfg: dict) -> "_SlowStubLLM":  # pragma: no cover
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        for i in range(self._n):
            time.sleep(self._delay)
            yield TextChunk(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=i,
                text=f"chunk {i}.",
                is_final=(i == self._n - 1),
            )


class _FastStubLLM(LLMEngine):
    """LLM stub that yields a single TextChunk instantly (for 409 tests)."""

    name = "fast-stub-llm"

    def __init__(self) -> None:
        pass

    @classmethod
    def from_config(cls, cfg: dict) -> "_FastStubLLM":  # pragma: no cover
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        yield TextChunk(
            session_id=session_id,
            utterance_id=utterance_id,
            seq=0,
            text="Quick reply.",
            is_final=True,
        )


class _StubTTS(TTSEngine):
    """TTS stub that yields one short AudioWindow per phrase TextChunk."""

    name = "stub-tts"
    sample_rate = 24000

    def __init__(self) -> None:
        self._seq = 0

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubTTS":  # pragma: no cover
        return cls()

    def synthesize(self, req: TTSRequest) -> AudioChunk:  # pragma: no cover
        raise RuntimeError("stub: use stream_audio()")

    def stream_audio(self, text_or_chunk, *, session_id="", utterance_id="", req=None,
                     min_ms=500, target_ms=1000, max_ms=2000) -> Iterator[AudioWindow]:
        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
        sid = text_or_chunk.session_id if isinstance(text_or_chunk, TextChunk) else session_id
        uid = text_or_chunk.utterance_id if isinstance(text_or_chunk, TextChunk) else utterance_id
        is_final = text_or_chunk.is_final if isinstance(text_or_chunk, TextChunk) else True
        aw = AudioWindow(
            session_id=sid,
            utterance_id=uid,
            seq=self._seq,
            sample_rate=24000,
            duration_ms=100,
            pcm=b"\x01\x00" * 2400,
            is_final=is_final,
            text_span=text,
        )
        self._seq += 1
        yield aw


# ---------- fixtures ----------


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")


def _deps_with_stubs(llm: LLMEngine | None = None, tts: TTSEngine | None = None,
                     backend: RenderBackend | None = None) -> v1.V1Deps:
    """Build V1Deps with mock backend + a stub-loaded EngineManager.

    The EngineManager is populated with the given llm/tts so /lite/say in mock
    mode can pick them up via ``engine_manager.llm / .tts``.
    """
    backend = backend or MockRenderBackend()
    em = EngineManager()
    if llm is not None:
        em._llm = llm
        em._llm_cfg = {"engine": llm.name}
    if tts is not None:
        em._tts = tts
        em._tts_cfg = {"engine": tts.name, "sample_rate": 24000}
    return v1.V1Deps(
        backend=backend,
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        director=None,
        engine_manager=em,
    )


def _prod_cfg(tokens: bool = False) -> AppConfig:
    return AppConfig(
        render_backend="mock",
        app_env="dev",  # auth disabled in dev
        backend_api_token="" if not tokens else "viewer-secret",
        admin_api_token="" if not tokens else "admin-secret",
        debug_enabled=False,
        cors_origins="*",
    )


# ---------- concurrent /lite/say -> one 200, one 409 ----------


async def test_concurrent_say_one_200_one_409(mock_env: None):
    """Two simultaneous /lite/say on the same session: one wins (200), the other
    is rejected as already_speaking (409)."""
    from core.server import create_app

    backend = MockRenderBackend()
    deps = _deps_with_stubs(llm=_SlowStubLLM(n_chunks=10, delay_s=0.02), tts=_StubTTS(), backend=backend)
    app = create_app(config=_prod_cfg(), deps=deps)

    # Start a session.
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = await client.post("/api/v1/lite/start", json={"avatar_id": None, "is_sandbox": True})
        assert start.status_code == 200
        sid = start.json()["session_id"]

        # Fire two says concurrently on that session.
        r1, r2 = await asyncio.gather(
            client.post("/api/v1/lite/say", json={"session_id": sid, "text": "first"}),
            client.post("/api/v1/lite/say", json={"session_id": sid, "text": "second"}),
        )
        codes = sorted([r1.status_code, r2.status_code])
        assert codes == [200, 409], f"expected [200, 409], got {codes}"
        # The 409 body must carry already_speaking detail.
        err = r1 if r1.status_code == 409 else r2
        assert "already_speaking" in err.json().get("detail", "") or err.json().get("detail") == "already_speaking"


async def test_lock_released_after_say_completes(mock_env: None):
    """After a /lite/say completes (200), a subsequent say on the same session
    succeeds (lock was released in finally)."""
    from core.server import create_app

    backend = MockRenderBackend()
    deps = _deps_with_stubs(llm=_FastStubLLM(), tts=_StubTTS(), backend=backend)
    app = create_app(config=_prod_cfg(), deps=deps)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = await client.post("/api/v1/lite/start", json={})
        sid = start.json()["session_id"]

        first = await client.post("/api/v1/lite/say", json={"session_id": sid, "text": "one"})
        assert first.status_code == 200

        second = await client.post("/api/v1/lite/say", json={"session_id": sid, "text": "two"})
        assert second.status_code == 200, (
            f"second say after first completed must succeed, got {second.status_code}: {second.text}"
        )


# ---------- /lite/interrupt during a long say ----------


async def test_interrupt_during_long_say_returns_200(mock_env: None):
    """A slow LLM stream is running; /lite/interrupt arrives mid-stream and
    returns 200. The original say completes (cancelled)."""
    from core.server import create_app

    backend = MockRenderBackend()
    deps = _deps_with_stubs(
        llm=_SlowStubLLM(n_chunks=100, delay_s=0.03),
        tts=_StubTTS(),
        backend=backend,
    )
    app = create_app(config=_prod_cfg(), deps=deps)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = await client.post("/api/v1/lite/start", json={})
        sid = start.json()["session_id"]

        # Start a long say in a task.
        say_task = asyncio.create_task(
            client.post("/api/v1/lite/say", json={"session_id": sid, "text": "long"})
        )
        # Let it start producing.
        await asyncio.sleep(0.10)

        # Interrupt mid-stream.
        intr = await client.post("/api/v1/lite/interrupt", json={"session_id": sid})
        assert intr.status_code == 200, f"interrupt must return 200, got {intr.status_code}"

        # Wait for the say to complete (it should not hang).
        say_resp = await asyncio.wait_for(say_task, timeout=10.0)
        assert say_resp.status_code in (200, 409), (
            f"say should complete (200) or be rejected (409), got {say_resp.status_code}"
        )


# ---------- cloud backend unchanged: /lite/say still calls backend.say ----------


class _FakeCloudBackend(FullPipelineBackend):
    """A fake non-mock backend so /lite/say takes the cloud path (backend.say)."""

    name = "fake-cloud"

    def __init__(self) -> None:
        self.said: list[tuple[str, str, bool]] = []

    def start(self, opts: StartOptions) -> StartResult:
        return StartResult(
            session_id="fake-cloud-session",
            livekit_url="mock://cloud",
            livekit_client_token="tok",
            mode="LITE",
        )

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        self.said.append((session_id, text, generate))
        return f"reply:{text}"

    def interrupt(self, session_id: str) -> None:
        return None

    def stop(self, session_id: str) -> None:
        return None


async def test_cloud_backend_say_path_unchanged(mock_env: None):
    """For a non-mock backend, /lite/say must call backend.say() (NOT the
    streaming coordinator path)."""
    from core.server import create_app

    cloud = _FakeCloudBackend()
    deps = _deps_with_stubs(llm=_FastStubLLM(), tts=_StubTTS(), backend=cloud)
    app = create_app(config=_prod_cfg(), deps=deps)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = await client.post("/api/v1/lite/start", json={})
        sid = start.json()["session_id"]

        r = await client.post("/api/v1/lite/say", json={"session_id": sid, "text": "hello"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["reply"] == "reply:hello"
        # The compatibility default still asks the cloud backend to generate.
        assert ("fake-cloud-session", "hello", True) in cloud.said


async def test_cloud_manual_say_forwards_verbatim_mode(mock_env: None):
    from core.server import create_app

    cloud = _FakeCloudBackend()
    deps = _deps_with_stubs(llm=_FastStubLLM(), tts=_StubTTS(), backend=cloud)
    app = create_app(config=_prod_cfg(), deps=deps)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = await client.post("/api/v1/lite/start", json={})
        sid = start.json()["session_id"]
        response = await client.post(
            "/api/v1/lite/say",
            json={"session_id": sid, "text": "Nói nguyên văn câu này.", "generate": False},
        )

    assert response.status_code == 200
    assert ("fake-cloud-session", "Nói nguyên văn câu này.", False) in cloud.said


async def test_streaming_manual_say_bypasses_llm(mock_env: None):
    from core.server import create_app

    class FailingLLM(_FastStubLLM):
        def stream_chunks(self, req, *, session_id="", utterance_id=""):
            raise AssertionError("manual verbatim speech must not call the LLM")
            yield

    backend = MockRenderBackend()
    deps = _deps_with_stubs(llm=FailingLLM(), tts=_StubTTS(), backend=backend)
    app = create_app(config=_prod_cfg(), deps=deps)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sid = (await client.post("/api/v1/lite/start", json={})).json()["session_id"]
        response = await client.post(
            "/api/v1/lite/say",
            json={"session_id": sid, "text": "Ba trăm năm mươi nghìn đồng.", "generate": False},
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "Ba trăm năm mươi nghìn đồng."
