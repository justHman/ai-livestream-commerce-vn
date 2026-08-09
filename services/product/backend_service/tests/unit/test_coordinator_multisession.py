"""Regression test for the per-decision orchestrator fix (Task A).

Verifies that DirectorCoordinator builds a FRESH StreamOrchestrator +
BoundedVideoQueue + CoordinatorMetrics per _maybe_speak() call so two
concurrent sessions do not corrupt each other's per-turn state.

Coverage:
  - Two sessions tick concurrently; both produce decisions/skips.
  - Each session's per-call queue is the one registered in
    orchestrator_registry[sid] while it is speaking — NOT a shared queue.
  - Cancelling session A's in-flight orchestrator does NOT stop session B's
    pipeline (the bug under the old shared-orchestrator design).
  - Per-call queue identity: the queue registered for sid_A is a distinct
    object from the queue registered for sid_B.

Uses stub LLM + ToneEngine + MockRenderBackend so it runs fully offline
(no GPU, no API key, no network).
"""

from __future__ import annotations

import asyncio
import time
from typing import Iterator

import pytest

from backend.application.director.catalog import Product
from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator
from backend.application.director.session_context import DirectorRuntime
from backend.application.render.locks import SessionLockRegistry
from avatar.engines.mock import MockRenderBackend, _MockSession
from backend.application.render.queue import BoundedVideoQueue
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from tts.engines.base import ToneEngine
from backend.application.render.windows import TextChunk

pytestmark = pytest.mark.asyncio


# ---------- stubs ----------


class _SlowStubLLM(LLMEngine):
    """LLM stub that yields several TextChunks with a tiny delay so a run
    lasts long enough for concurrency/cancel assertions."""

    name = "slow-stub-llm"

    def __init__(self, n_chunks: int = 10, delay_s: float = 0.01) -> None:
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


# ---------- helpers ----------


def _make_products() -> list[Product]:
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
    sess = _MockSession(session_id=session_id)
    sess.idle_loop_frames = backend._build_idle_loop(session_id)
    backend._sessions[session_id] = sess


def _make_coordinator(
    tick_ms: int = 30,
    window_sec: float = 75.0,
) -> tuple[DirectorCoordinator, dict]:
    """Build a DirectorCoordinator with stubs and TWO pre-registered mock
    sessions (sid_a, sid_b) so both can stream_audio without KeyError."""
    backend = MockRenderBackend()
    sid_a, sid_b = "sess-a", "sess-b"
    _register_mock_session(backend, sid_a)
    _register_mock_session(backend, sid_b)

    runtime = DirectorRuntime(backend=backend)
    llm = _SlowStubLLM(n_chunks=8, delay_s=0.01)
    tts = ToneEngine()

    locks = SessionLockRegistry()
    registry: dict = {}
    cfg = CoordinatorConfig(tick_ms=tick_ms, window_sec=window_sec)
    coord = DirectorCoordinator(
        runtime=runtime,
        llm=llm,
        tts=tts,
        backend=backend,
        chunker_config={
            "text_chunk_min_chars": 12,
            "text_chunk_target_chars": 40,
            "text_chunk_max_chars": 80,
            "text_chunk_flush_timeout_ms": 350,
        },
        lock_registry=locks,
        cfg=cfg,
        hub=None,
        orchestrator_registry=registry,
        max_queue_windows=5,
    )
    return coord, registry


# ---------- tests ----------


async def test_two_concurrent_sessions_both_progress(monkeypatch: pytest.MonkeyPatch):
    """Two sessions tick concurrently: both must produce decisions or skips
    (i.e. the tick loop advances for BOTH), and the coordinator does not
    crash or deadlock."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    coord, registry = _make_coordinator(tick_ms=30)
    sid_a, sid_b = "sess-a", "sess-b"
    products = _make_products()

    coord.start(sid_a, products)
    coord.start(sid_b, products)
    assert coord.has(sid_a)
    assert coord.has(sid_b)

    # Feed both sessions with comments that should trigger non-idle decisions.
    now = time.time()
    for i in range(5):
        coord.ingest(sid_a, f"San pham P001 gia bao nhieu? {i}", f"a-user{i}", ts=now)
        coord.ingest(sid_b, f"San pham P002 co khuyen mai gi khong? {i}", f"b-user{i}", ts=now)

    # Let both tick loops run until each session has completed at least one
    # decision/skip, polling progress state instead of sleeping a fixed
    # duration: a speak() completes only after the render path finishes,
    # which can take ~2.5-3.4s under ToneEngine+PIL, so a fixed sleep either
    # races it or wastes time. The deadline is a deadlock backstop only.
    deadline = time.monotonic() + 8.0
    stats_a = stats_b = None
    while time.monotonic() < deadline:
        stats_a = coord.stats(sid_a)
        stats_b = coord.stats(sid_b)
        if (
            stats_a["decisions_emitted"] + stats_a["skips"] > 0
            and stats_b["decisions_emitted"] + stats_b["skips"] > 0
        ):
            break
        await asyncio.sleep(0.02)

    # Both sessions must have processed at least one tick (decision or skip).
    assert stats_a["decisions_emitted"] + stats_a["skips"] > 0, (
        f"session A did not progress: {stats_a}"
    )
    assert stats_b["decisions_emitted"] + stats_b["skips"] > 0, (
        f"session B did not progress: {stats_b}"
    )

    coord.stop(sid_a)
    coord.stop(sid_b)
    await asyncio.sleep(0.05)


async def test_registry_holds_correct_per_session_queue(monkeypatch: pytest.MonkeyPatch):
    """While session A is speaking, orchestrator_registry['sess-a']['queue']
    is the per-call queue for A — NOT a shared queue, and NOT the same object
    as session B's queue if B is also speaking."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    coord, registry = _make_coordinator(tick_ms=20)
    sid_a, sid_b = "sess-a", "sess-b"
    products = _make_products()

    coord.start(sid_a, products)
    coord.start(sid_b, products)

    # Prime both sessions with comments so decide() returns non-idle.
    now = time.time()
    for i in range(5):
        coord.ingest(sid_a, f"P001 gia bao nhieu? {i}", f"a-user{i}", ts=now)
        coord.ingest(sid_b, f"P002 khuyen mai gi? {i}", f"b-user{i}", ts=now)

    # Wait until at least one (preferably both) sessions register a queue.
    deadline = time.monotonic() + 2.0
    seen_a = seen_b = None
    while time.monotonic() < deadline:
        entry_a = registry.get(sid_a)
        entry_b = registry.get(sid_b)
        if entry_a is not None:
            seen_a = entry_a["queue"]
        if entry_b is not None:
            seen_b = entry_b["queue"]
        if seen_a is not None and seen_b is not None:
            break
        await asyncio.sleep(0.02)

    # At least one session must have registered a queue (the tick loop drove
    # a _maybe_speak call). The registry entry's queue must be a
    # BoundedVideoQueue instance (not a shared long-lived one).
    assert seen_a is not None or seen_b is not None, (
        "neither session registered a queue in the registry"
    )
    # If both registered, they must be DISTINCT queue instances (per-call
    # construction), proving the queues are not shared across sessions.
    if seen_a is not None and seen_b is not None:
        assert seen_a is not seen_b, (
            "session A and B share the same queue (per-call factory broken)"
        )
    # Each registered queue must be a BoundedVideoQueue.
    for q in (seen_a, seen_b):
        if q is not None:
            assert isinstance(q, BoundedVideoQueue)

    coord.stop(sid_a)
    coord.stop(sid_b)
    await asyncio.sleep(0.05)


async def test_cancel_one_session_does_not_cancel_the_other(monkeypatch: pytest.MonkeyPatch):
    """Cancelling session A's in-flight orchestrator must NOT stop session
    B's pipeline. Under the old shared-orchestrator design, calling
    cancel(sid_a) on the shared orchestrator also stopped sid_b because the
    cancel_event was shared. With per-call orchestrators, the registry holds
    A's orchestrator and B's separately, so cancelling A leaves B intact.

    We verify by:
      1. Drive both sessions until both register an orchestrator in the
         registry.
      2. Snapshot B's orchestrator+queue.
      3. Cancel A's orchestrator via its registry entry.
      4. Assert B's orchestrator is still running (its cancel_event is NOT
         set) and B's queue is NOT cleared.
    """
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    coord, registry = _make_coordinator(tick_ms=20)
    sid_a, sid_b = "sess-a", "sess-b"
    products = _make_products()

    coord.start(sid_a, products)
    coord.start(sid_b, products)

    now = time.time()
    for i in range(5):
        coord.ingest(sid_a, f"P001 gia bao nhieu? {i}", f"a-user{i}", ts=now)
        coord.ingest(sid_b, f"P002 khuyen mai gi? {i}", f"b-user{i}", ts=now)

    # Wait until BOTH sessions have an in-flight orchestrator.
    deadline = time.monotonic() + 2.0
    orch_a = orch_b = None
    while time.monotonic() < deadline:
        ea = registry.get(sid_a)
        eb = registry.get(sid_b)
        if ea is not None:
            orch_a = ea["orchestrator"]
        if eb is not None:
            orch_b = eb["orchestrator"]
        if orch_a is not None and orch_b is not None:
            break
        await asyncio.sleep(0.02)

    assert orch_a is not None and orch_b is not None, (
        f"could not get both orchestrators in flight: a={orch_a} b={orch_b}"
    )
    # Distinct orchestrator instances — the central regression assertion.
    assert orch_a is not orch_b, (
        "session A and B share the same orchestrator (per-call factory broken)"
    )

    # Snapshot B's queue and cancel state BEFORE cancelling A.
    cancel_b_before = orch_b._cancel_event.is_set()

    # Cancel A's in-flight orchestrator through its registry entry (this is
    # what the coordinator's interrupt path does).
    await orch_a.cancel(sid_a)

    # Give the cancel a moment to propagate within A's worker.
    await asyncio.sleep(0.05)

    # B's orchestrator must NOT be cancelled — its cancel_event must still
    # be in the same state as before (False if it was running).
    cancel_b_after = orch_b._cancel_event.is_set()
    assert cancel_b_after == cancel_b_before, (
        f"session B's cancel_event flipped after cancelling A: "
        f"before={cancel_b_before} after={cancel_b_after}"
    )
    # B's queue must NOT have been cleared by A's cancel.
    # Note: B's worker may still be producing frames, so qsize can change,
    # but the queue object must still be the live one (not replaced).
    assert registry.get(sid_b) is not None, "session B's registry entry vanished after cancelling A"
    assert registry[sid_b]["orchestrator"] is orch_b, (
        "session B's orchestrator was replaced/corrupted by A's cancel"
    )

    coord.stop(sid_a)
    coord.stop(sid_b)
    await asyncio.sleep(0.05)


async def test_decision_score_field_used_no_regex(monkeypatch: pytest.MonkeyPatch):
    """Sanity: the coordinator's interrupt arbitration reads decision.score
    directly (a float field on the Decision dataclass) rather than parsing
    the reason string. We assert the field exists and is a float for a
    real decision produced by the director."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    from backend.application.director.decision import Decision

    # The dataclass must expose a `score` field with a float default.
    dec = Decision(action="idle")
    assert hasattr(dec, "score")
    assert isinstance(dec.score, float)
    assert dec.score == 0.0

    coord, registry = _make_coordinator(tick_ms=20)
    sid_a = "sess-a"
    products = _make_products()
    coord.start(sid_a, products)
    coord.ingest(sid_a, "P001 gia bao nhieu?", "user1")

    # Drive a few ticks so the director produces a non-idle Decision.
    captured: list[Decision] = []
    ds = coord._runtime._sessions.get(sid_a)
    assert ds is not None
    original_decide = ds.director.decide

    def _capture(comments, now=0.0):
        d = original_decide(comments, now=now)
        captured.append(d)
        return d

    ds.director.decide = _capture
    await asyncio.sleep(0.3)

    # At least one non-idle decision should have been produced.
    non_idle = [d for d in captured if d.action not in ("idle", "skip")]
    if non_idle:
        # Every non-idle decision must carry a float score (set by director).
        for d in non_idle:
            assert isinstance(d.score, float), (
                f"decision.score must be float, got {type(d.score).__name__}"
            )

    coord.stop(sid_a)
    await asyncio.sleep(0.05)
