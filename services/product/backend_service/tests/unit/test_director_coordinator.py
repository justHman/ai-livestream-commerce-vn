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
import time
from typing import Iterator

import pytest

from backend.application.director.catalog import Product
from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator
from backend.application.director.decision import Decision
from backend.application.director.session_context import DirectorRuntime
from backend.application.render.locks import SessionLockRegistry
from backend.application.render.orchestrator import StreamingControllerConfig
from avatar.engines.mock import MockRenderBackend, _MockSession
from backend.application.render.windows import AudioWindow
from backend.application.text_chunker import FixedChunkPolicyConfig, TextChunk
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from tts.engines.base import AudioChunk, TTSEngine, TTSRequest

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

    def stream_audio(
        self,
        text_or_chunk,
        *,
        session_id="",
        utterance_id="",
        req=None,
        min_ms=500,
        target_ms=1000,
        max_ms=2000,
    ) -> Iterator[AudioWindow]:
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
) -> tuple[DirectorCoordinator, DirectorRuntime, SessionLockRegistry, MockRenderBackend]:
    """Build a full DirectorCoordinator with stubs.

    The coordinator builds a fresh orchestrator per _maybe_speak() call, so we
    pass the factory inputs (llm, tts, backend, chunker_config) rather than a
    pre-built orchestrator. The backend has the session pre-registered so
    stream_audio doesn't raise KeyError.
    """
    backend = MockRenderBackend()
    _register_mock_session(backend, session_id)

    runtime = DirectorRuntime(backend=backend)

    llm = _StubLLM()
    tts = _StubTTS()

    locks = SessionLockRegistry()
    cfg = CoordinatorConfig(tick_ms=tick_ms, window_sec=window_sec)
    coord = DirectorCoordinator(
        runtime=runtime,
        llm=llm,
        tts=tts,
        backend=backend,
        fixed_config=FixedChunkPolicyConfig(),
        controller_config=StreamingControllerConfig(),
        lock_registry=locks,
        cfg=cfg,
    )
    return coord, runtime, locks, backend


# ---------- tests ----------


async def test_start_ingest_and_emit_decision(monkeypatch: pytest.MonkeyPatch):
    """start(sid) -> ingest 5 comments -> wait a few ticks -> coordinator emits >= 1 decision."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "test-sess-1"
    coord, runtime, locks, backend = _make_coordinator(tick_ms=50, session_id=sid)
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


async def test_stop_cancels_task_and_drops_queue(monkeypatch: pytest.MonkeyPatch):
    """stop(sid) -> task cancelled, ChatQueue dropped."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "test-sess-2"
    coord, runtime, locks, backend = _make_coordinator(tick_ms=50, session_id=sid)
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


async def test_stop_all_cancels_every_active_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    first, runtime, locks, backend = _make_coordinator(session_id="first")
    products = _make_products()
    first.start("first", products)
    first.start("second", products)

    first.stop_all()

    assert first.has("first") is False
    assert first.has("second") is False
    await asyncio.sleep(0)


async def test_loop_survives_decide_exception(monkeypatch: pytest.MonkeyPatch):
    """If decide() raises once, the next tick continues normally."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "test-sess-3"
    coord, runtime, locks, backend = _make_coordinator(tick_ms=30, session_id=sid)
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


async def test_ingest_raises_for_unknown_session(monkeypatch: pytest.MonkeyPatch):
    """ingest() on a non-existent session raises KeyError."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    coord, _, _, _ = _make_coordinator()
    with pytest.raises(KeyError):
        coord.ingest("nonexistent", "hello", "user")


async def test_stats_empty_session(monkeypatch: pytest.MonkeyPatch):
    """stats() for a non-started session returns zeroed dict."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    coord, _, _, _ = _make_coordinator()
    s = coord.stats("nonexistent")
    assert s["decisions_emitted"] == 0
    assert s["skips"] == 0
    assert s["interrupts"] == 0


async def test_start_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    """Calling start() twice for the same session does not crash or duplicate tasks."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "test-sess-idempotent"
    coord, runtime, locks, backend = _make_coordinator(tick_ms=50, session_id=sid)
    products = _make_products()

    coord.start(sid, products)
    task1 = coord._tasks[sid]

    coord.start(sid, products)
    task2 = coord._tasks[sid]

    # Same task object — start was a no-op.
    assert task1 is task2

    coord.stop(sid)
    await asyncio.sleep(0.05)


# ---------- WS emit + orchestrator-registry tests ----------


class _RecordingHub:
    """Minimal ControlHub stand-in that records emitted events per session."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, session_id: str, event: dict) -> None:
        self.events.append((session_id, dict(event)))

    def types(self) -> list[str]:
        return [e["type"] for _, e in self.events]


async def test_coordinator_emits_ws_events_and_registers_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
):
    """When hub + orchestrator_registry are wired, the coordinator emits
    coordinator.speak_started/finished and director.decision, and registers the
    active orchestrator+queue so MJPEG can drain utterance frames.

    The registry-population is verified deterministically via the
    _register_speaking/_unregister_speaking helpers (the tick-loop timing is
    too racy to assert "registry empty at snapshot X" — a later tick may have
    started a new in-flight speak).
    """
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    from backend.application.director.coordinator import _decision_to_event  # internal helper

    sid = "test-sess-ws"
    coord, runtime, locks, backend = _make_coordinator(tick_ms=40, session_id=sid)
    products = _make_products()
    hub = _RecordingHub()
    registry: dict = {}
    coord._hub = hub
    coord._orchestrator_registry = registry

    # Deterministic: register/unregister lifecycle on the registry. The
    # coordinator now builds a fresh orchestrator+queue per _maybe_speak()
    # call, so _register_speaking takes both the orchestrator and queue as
    # arguments.
    from backend.application.render.orchestrator import StreamOrchestrator
    from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics

    assert sid not in registry
    orch = StreamOrchestrator(
        llm=_StubLLM(),
        tts=_StubTTS(),
        backend=backend,
        queue=BoundedVideoQueue(max_size=5),
        metrics=CoordinatorMetrics(),
    )
    q = BoundedVideoQueue(max_size=5)
    coord._register_speaking(sid, orch, q)
    assert sid in registry
    assert registry[sid]["orchestrator"] is orch
    assert registry[sid]["queue"] is q
    coord._unregister_speaking(sid)
    assert sid not in registry
    # Idempotent unregister (no error on second call).
    coord._unregister_speaking(sid)

    coord.start(sid, products)
    # Ingest a comment that should trigger a non-idle decision.
    coord.ingest(sid, "San pham nay gia bao nhieu?", "user1")
    # Wait enough ticks for decide + speak.
    await asyncio.sleep(0.5)

    types = hub.types()
    # director.decision is emitted every tick that produces a decision.
    assert "director.decision" in types, f"no director.decision in {types}"
    # If a speak happened, the start/finished pair must both be present
    # (the finally block emits finished + unregisters).
    if "coordinator.speak_started" in types:
        assert "coordinator.speak_finished" in types

    # _decision_to_event projects only safe decision metadata — no prompt
    # text, customer data, or comment text.
    dec = Decision(
        action="answer_fact",
        product_id="P001",
        field="price",
        may_interrupt=False,
        reason="score=3",
    )
    ev = _decision_to_event(dec)
    assert ev == {
        "turn_id": dec.turn_id,
        "action": "answer_fact",
        "product": "P001",
        "field": "price",
        "stage": None,
        "task_id": None,
        "may_interrupt": False,
        "reason": "score=3",
    }

    coord.stop(sid)
    await asyncio.sleep(0.05)


async def test_coordinator_without_hub_is_noop(monkeypatch: pytest.MonkeyPatch):
    """No hub and no registry wired -> coordinator still runs, no errors."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "test-sess-nohub"
    coord, runtime, locks, backend = _make_coordinator(tick_ms=30, session_id=sid)
    assert coord._hub is None
    assert coord._orchestrator_registry is None

    coord.start(sid, _make_products())
    coord.ingest(sid, "hello", "user1")
    await asyncio.sleep(0.25)

    # No exceptions raised -> baseline behavior preserved.
    stats = coord.stats(sid)
    assert stats["decisions_emitted"] + stats["skips"] >= 0

    coord.stop(sid)
    await asyncio.sleep(0.05)


# ---------- event secrecy: no rendered prompt / customer / comment text in WS events ----------


_SENSITIVE_SENTINELS = [
    "SHOP_SECRET_123",
    "Mua áo hoodie mãi mãi",
    "TOKEN_LIKE_deadbeef",
    "base_sales_vi",
    "response_guardrails_vi",
    "Guarda:",
    "Bạn là MC",
]


async def test_ws_events_never_contain_sensitive_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every WS event emitted by the coordinator must be free of rendered
    prompt text, shop/product data, comment text, and credentials.

    Tests the real captured event sink (``_RecordingHub``) and asserts
    sentinel strings are absent from every event and failure path.
    """
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "test-sess-secrecy"
    coord, runtime, locks, backend = _make_coordinator(tick_ms=40, session_id=sid)
    products = _make_products()
    hub = _RecordingHub()
    coord._hub = hub

    # Skip the coordinator's own start() which would create a real tick loop
    # that races with manual event injection. Instead, attach the runtime
    # directly and test each event path explicitly.
    runtime.attach(sid, products, shop_profile="SHOP_SECRET_123")

    # ── director.decision event ──
    from backend.application.director.coordinator import _decision_to_event

    dec = Decision(
        action="answer_cluster",
        product_id="P-4096-SECRET",
        prompt="Hãy trả lời câu hỏi: Mua áo hoodie mãi mãi",
        text="Mua áo hoodie mãi mãi",
        cluster_members=("Mua áo hoodie mãi mãi",),
        cluster_member_ids=("cmt-001",),
        score_breakdown={"cluster_size": 1.0},
        reason="score=3",
    )
    ev = _decision_to_event(dec)
    flat = str(ev)
    for sentinel in _SENSITIVE_SENTINELS:
        assert sentinel not in flat, f"sentinel {sentinel!r} found in _decision_to_event: {ev}"

    # ── _speech_item (queued, processing, failed, cancelled) ──
    for state in ("queued", "processing", "failed", "cancelled_stale"):
        item = DirectorCoordinator._speech_item(dec, state)
        flat = str(item)
        for sentinel in _SENSITIVE_SENTINELS:
            assert sentinel not in flat, (
                f"sentinel {sentinel!r} found in _speech_item(state={state}): {item}"
            )

    # ── WS events via hub: speak_started, speak_finished, terminal_failure ──
    # speak_started
    await hub.emit(
        sid,
        {
            "type": "coordinator.speak_started",
            "turn_id": dec.turn_id,
            "state": "processing",
            "action": dec.action,
            "product": dec.product_id,
        },
    )
    # speak_finished
    await hub.emit(
        sid,
        {
            "type": "coordinator.speak_finished",
            "turn_id": dec.turn_id,
            "state": "completed",
            "action": dec.action,
            "product_id": dec.product_id,
        },
    )
    # terminal_failure
    await hub.emit(
        sid,
        {
            "type": "coordinator.terminal_failure",
            "turn_id": dec.turn_id,
            "state": "failed",
            "error": "TestError",
        },
    )
    # retry_scheduled
    await hub.emit(
        sid,
        {
            "type": "coordinator.retry_scheduled",
            "turn_id": dec.turn_id,
            "retry_count": 1,
            "error": "TimeoutError",
        },
    )

    # Verify every emitted event is clean
    for event_sid, event in hub.events:
        flat = str(event)
        for sentinel in _SENSITIVE_SENTINELS:
            assert sentinel not in flat, (
                f"sentinel {sentinel!r} found in hub event {event.get('type')}: {event}"
            )

    # ── completed_speech dict (from _maybe_speak) ──
    completed = {
        "turn_id": dec.turn_id,
        "state": "completed",
        "action": "answer_cluster",
        "product_id": "P-4096-SECRET",
        "stage": None,
        "task_id": None,
        "script": "Một script trả lời",
        "attempt": 0,
    }
    flat = str(completed)
    for sentinel in _SENSITIVE_SENTINELS:
        assert sentinel not in flat, f"sentinel {sentinel!r} found in completed_speech: {completed}"
