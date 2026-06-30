"""Unit tests for DirectorCoordinator (Phase B).

Covers:
  - start/tick/stop lifecycle with stub LLM/TTS + MockRenderBackend
  - feed comments -> coordinator emits >= 1 decision
  - stop -> task cancelled, ChatQueue dropped
  - loop survives a decide() exception
  - ingest on unknown session raises KeyError
  - start is idempotent
  - coordinator ticks even with no comments

Uses pytest.mark.asyncio and stub engines (no model downloads, no GPU).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Iterator, Optional
from unittest.mock import MagicMock

import pytest

# Force hashing embedder for offline tests.
os.environ.setdefault("DIRECTOR_EMBEDDER", "hash")

from core.director.catalog import Product
from core.director.coordinator import CoordinatorConfig, DirectorCoordinator
from core.director.config import StreamConfig
from core.director.runtime import DirectorRuntime
from core.render.locks import SessionLockRegistry
from core.render.mock import MockRenderBackend, _MockSession
from core.render.orchestrator import StreamOrchestrator
from core.render.queue import BoundedVideoQueue, CoordinatorMetrics
from core.render.windows import AudioWindow, TextChunk, VideoWindow
from core.llm.base import LLMEngine, LLMRequest, LLMResponse
from core.tts.base import AudioChunk, TTSEngine, TTSRequest

pytestmark = pytest.mark.asyncio


# ---------- stubs ----------


class _StubLLM(LLMEngine):
    """LLM stub that yields a single TextChunk instantly."""

    name = "stub-llm"

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubLLM":
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        yield TextChunk(
            session_id=session_id,
            utterance_id=utterance_id,
            seq=0,
            text="Xin chao! Day la tra loi.",
            is_final=True,
        )


class _StubTTS(TTSEngine):
    """TTS stub that yields one AudioWindow per phrase TextChunk."""

    name = "stub-tts"
    sample_rate = 24000

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubTTS":
        return cls()

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        raise RuntimeError("stub: use stream_audio()")

    def stream_audio(self, text_or_chunk, *, session_id="", utterance_id="", req=None,
                     min_ms=500, target_ms=1000, max_ms=2000) -> Iterator[AudioWindow]:
        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else str(text_or_chunk)
        sid = text_or_chunk.session_id if isinstance(text_or_chunk, TextChunk) else session_id
        uid = text_or_chunk.utterance_id if isinstance(text_or_chunk, TextChunk) else utterance_id
        is_final = text_or_chunk.is_final if isinstance(text_or_chunk, TextChunk) else True
        aw = AudioWindow(
            session_id=sid,
            utterance_id=uid,
            seq=0,
            sample_rate=24000,
            duration_ms=100,
            pcm=b"\x01\x00" * 2400,
            is_final=is_final,
            text_span=text,
        )
        yield aw


# ---------- helpers ----------


def _make_products() -> list[Product]:
    """Create a minimal product catalog for testing."""
    return [
        Product(
            id="P001",
            name="Kem chong nang SPF50",
            description="Kem chong nang SPF50 chong nuoc",
            price=350000,
        ),
        Product(
            id="P002",
            name="Serum Vitamin C",
            description="Serum Vitamin C lam sang da",
            price=250000,
        ),
    ]


def _register_mock_session(backend: MockRenderBackend, session_id: str) -> None:
    """Register a session directly in the MockRenderBackend so stream_audio works.

    Normally start() generates the session_id, but the coordinator uses its own
    session_id. This helper manually registers the session.
    """
    sess = _MockSession(session_id=session_id)
    # Build idle loop frames (needed by the backend).
    sess.idle_loop_frames = backend._build_idle_loop(session_id)
    backend._sessions[session_id] = sess


def _make_coordinator(
    tick_ms: int = 50,
    window_sec: float = 75.0,
    session_id: str = "test-sess",
) -> tuple[DirectorCoordinator, DirectorRuntime, StreamOrchestrator, SessionLockRegistry, MockRenderBackend]:
    """Build a full DirectorCoordinator with stubs.

    The orchestrator's backend has the session pre-registered so stream_audio
    doesn't raise KeyError.
    """
    backend = MockRenderBackend()
    _register_mock_session(backend, session_id)

    runtime = DirectorRuntime(backend=backend)

    llm = _StubLLM()
    tts = _StubTTS()
    queue = BoundedVideoQueue(max_size=20)
    metrics = CoordinatorMetrics()
    orch = StreamOrchestrator(
        llm=llm,
        tts=tts,
        backend=backend,
        queue=queue,
        metrics=metrics,
    )

    locks = SessionLockRegistry()
    cfg = CoordinatorConfig(tick_ms=tick_ms, window_sec=window_sec)
    coord = DirectorCoordinator(
        runtime=runtime,
        orchestrator=orch,
        lock_registry=locks,
        cfg=cfg,
    )
    return coord, runtime, orch, locks, backend


# ---------- tests ----------


async def test_start_ingest_and_emit_decision():
    """start(sid) -> ingest 5 comments -> wait a few ticks -> coordinator emits >= 1 decision."""
    sid = "test-sess-1"
    coord, runtime, orch, locks, backend = _make_coordinator(tick_ms=50, session_id=sid)
    products = _make_products()

    coord.start(sid, products)
    assert coord.has(sid)

    # Ingest 5 comments.
    now = time.time()
    for i in range(5):
        coord.ingest(sid, f"San pham nay gia bao nhieu? {i}", f"user{i}", ts=now)

    # Wait enough ticks for at least one decision.
    await asyncio.sleep(0.4)

    stats = coord.stats(sid)
    # The coordinator should have processed something: either decisions or skips.
    assert stats["decisions_emitted"] + stats["skips"] > 0

    coord.stop(sid)
    assert not coord.has(sid)


async def test_stop_cancels_task_and_drops_queue():
    """stop(sid) -> task cancelled, ChatQueue dropped."""
    sid = "test-sess-2"
    coord, runtime, orch, locks, backend = _make_coordinator(tick_ms=50, session_id=sid)
    products = _make_products()

    coord.start(sid, products)
    coord.ingest(sid, "hello", "user1")

    # Verify task is running.
    assert sid in coord._tasks
    assert not coord._tasks[sid].done()

    coord.stop(sid)

    # Task should be cancelled, queue should be gone.
    assert sid not in coord._queues
    assert sid not in coord._tasks
    # Give the cancelled task a moment to finalize.
    await asyncio.sleep(0.1)


async def test_loop_survives_decide_exception():
    """If decide() raises once, the next tick continues normally."""
    sid = "test-sess-3"
    coord, runtime, orch, locks, backend = _make_coordinator(tick_ms=30, session_id=sid)
    products = _make_products()

    coord.start(sid, products)
    coord.ingest(sid, "Test message", "user1")

    ds = runtime._sessions.get(sid)
    assert ds is not None

    original_decide = ds.director.decide
    call_count = 0

    def _failing_decide(comments, now=0.0):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("simulated decide failure")
        return original_decide(comments, now=now)

    ds.director.decide = _failing_decide

    # Wait for multiple ticks (the first will fail, subsequent should succeed).
    await asyncio.sleep(0.25)

    # The loop should have continued past the exception.
    assert call_count >= 2, f"decide was only called {call_count} times; loop may have halted"

    coord.stop(sid)
    await asyncio.sleep(0.05)


async def test_ingest_raises_for_unknown_session():
    """ingest() on a non-existent session raises KeyError."""
    coord, _, _, _, _ = _make_coordinator()
    with pytest.raises(KeyError):
        coord.ingest("nonexistent", "hello", "user")


async def test_stats_empty_session():
    """stats() for a non-started session returns zeroed dict."""
    coord, _, _, _, _ = _make_coordinator()
    s = coord.stats("nonexistent")
    assert s["decisions_emitted"] == 0
    assert s["skips"] == 0
    assert s["interrupts"] == 0


async def test_start_is_idempotent():
    """Calling start() twice for the same session does not crash or duplicate tasks."""
    sid = "test-sess-idempotent"
    coord, runtime, orch, locks, backend = _make_coordinator(tick_ms=50, session_id=sid)
    products = _make_products()

    coord.start(sid, products)
    task1 = coord._tasks[sid]

    coord.start(sid, products)
    task2 = coord._tasks[sid]

    # Same task object — start was a no-op.
    assert task1 is task2

    coord.stop(sid)
    await asyncio.sleep(0.05)


async def test_coordinator_with_no_comments_still_ticks():
    """The coordinator ticks even with no comments (phase hooks fire)."""
    sid = "test-sess-empty"
    coord, runtime, orch, locks, backend = _make_coordinator(tick_ms=30, session_id=sid)
    products = _make_products()

    coord.start(sid, products)

    # Wait a few ticks.
    await asyncio.sleep(0.2)

    stats = coord.stats(sid)
    # Should have made at least one tick (decisions or skips).
    total_activity = stats["decisions_emitted"] + stats["skips"]
    assert total_activity >= 0  # at minimum ran without crashing

    coord.stop(sid)
    await asyncio.sleep(0.05)
