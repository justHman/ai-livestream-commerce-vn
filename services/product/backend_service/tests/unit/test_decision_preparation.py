"""Stage 2 diagnostics and cluster snapshot contracts."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from backend.application.director.catalog import Product
from backend.application.director.coordinator import DirectorCoordinator, _SessionStats
from backend.application.director.decision import Decision
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.session_context import DirectorRuntime
from backend.application.render.engines_base import FullPipelineBackend, StartOptions, StartResult
from avatar.engines.mock import MockRenderBackend, _MockSession


class _CountingEmbedder(HashingEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.encoded_batches: list[tuple[str, ...]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.encoded_batches.append(tuple(texts))
        return super().encode(texts)


class _BlockingCloudBackend(FullPipelineBackend):
    name = "blocking-cloud"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def start(self, opts: StartOptions) -> StartResult:
        return StartResult("sid", "wss://example", "token")

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        self.started.set()
        self.release.wait(timeout=2)
        return "Kịch bản đã phát xong."

    def interrupt(self, session_id: str) -> None:
        self.release.set()

    def stop(self, session_id: str) -> None:
        return None


class _FlakyCloudBackend(FullPipelineBackend):
    name = "flaky-cloud"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def start(self, opts: StartOptions) -> StartResult:
        return StartResult("sid", "wss://example", "token")

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("transient")
        return "Kịch bản phát thành công."

    def interrupt(self, session_id: str) -> None:
        return None

    def stop(self, session_id: str) -> None:
        return None


class _RecordingCloudBackend(FullPipelineBackend):
    name = "recording-cloud"

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def start(self, opts: StartOptions) -> StartResult:
        return StartResult("sid", "wss://example", "token")

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        self.calls.append((text, generate))
        return text

    def interrupt(self, session_id: str) -> None:
        return None

    def stop(self, session_id: str) -> None:
        return None


class _RecordingHub:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, session_id: str, event: dict) -> None:
        self.events.append(dict(event))


def test_chat_queue_snapshot_has_canonical_counts() -> None:
    from backend.application.director.comment_buffer import ChatQueue

    queue = ChatQueue("diagnostics", max_size=2)
    now = time.time()
    queue.put("old", "viewer", ts=now - 100)
    queue.put("fresh-1", "viewer", ts=now)
    queue.put("fresh-2", "viewer", ts=now)

    stats = queue.stats(window_sec=75, now=now)

    assert stats["received_total"] == 3
    assert stats["buffered_comments"] == 2
    assert stats["active_comments"] == 2
    assert stats["pending"] == stats["buffered_comments"]
    assert stats["total_put"] == stats["received_total"]
    assert [item.text for item in queue.snapshot()] == ["fresh-1", "fresh-2"]


@pytest.mark.asyncio
async def test_speech_turn_keeps_one_id_from_start_to_completed_history() -> None:
    backend = _BlockingCloudBackend()
    hub = _RecordingHub()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(
        runtime=runtime,
        llm=None,
        tts=None,
        backend=backend,
        hub=hub,
        completed_history_size=3,
    )
    session_id = "speech-lifecycle"
    coordinator._stats[session_id] = _SessionStats()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    decision = Decision(
        action="answer_cluster",
        product_id="P004",
        prompt="Prompt trả lời",
        cluster_members=("Áo hoodie giá bao nhiêu?",),
    )

    task = asyncio.create_task(coordinator._maybe_speak(session_id, decision))
    assert await asyncio.to_thread(backend.started.wait, 1)

    active = coordinator.stats(session_id)
    assert active["active_decision"]["turn_id"] == decision.turn_id
    assert active["active_decision"]["state"] == "processing"
    assert active["completed_speeches"] == 0
    assert active["queued_decisions"] == 0

    backend.release.set()
    await task

    finished = coordinator.stats(session_id)
    assert finished["active_decision"] is None
    assert finished["completed_speeches"] == 1
    assert finished["completed_speech_history"][0]["turn_id"] == decision.turn_id
    assert finished["completed_speech_history"][0]["script"] == "Kịch bản đã phát xong."
    lifecycle = [event for event in hub.events if event["type"].startswith("coordinator.speak_")]
    assert [event["turn_id"] for event in lifecycle] == [decision.turn_id, decision.turn_id]
    assert lifecycle[1]["action"] == "answer_cluster"
    assert lifecycle[1]["product_id"] == "P004"
    # No prompt/script text in WS events — only safe metadata.
    assert "script" not in lifecycle[1]


@pytest.mark.asyncio
async def test_reattach_keeps_active_turn_committable() -> None:
    backend = _BlockingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "reattach-active-turn"
    original = Product(id="P004", name="Áo hoodie")
    runtime.attach(session_id, [original], shop_profile="Shop cũ")
    coordinator._stats[session_id] = _SessionStats()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    decision = Decision(action="introduce_product", product_id="P004", prompt="Mở sản phẩm")
    decision.revision_token = runtime.current_generation_token(session_id)

    task = asyncio.create_task(coordinator._maybe_speak(session_id, decision))
    assert await asyncio.to_thread(backend.started.wait, 1)
    revised = Product(id="P004", name="Áo hoodie bản mới")
    runtime.attach(session_id, [revised], shop_profile="Shop mới")
    coordinator.update_catalog(session_id, [revised])
    backend.release.set()
    await task

    session = runtime.get_session(session_id)
    assert session.director.state.products[0].is_introduced is True
    assert coordinator.stats(session_id)["completed_speech_history"][-1]["state"] == "completed"


@pytest.mark.asyncio
async def test_transient_turn_retries_per_turn_and_advances_only_after_success() -> None:
    backend = _FlakyCloudBackend(failures=1)
    hub = _RecordingHub()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(
        runtime=runtime,
        llm=None,
        tts=None,
        backend=backend,
        hub=hub,
    )
    session_id = "retry-turn"
    runtime.attach(
        session_id,
        [Product(id="P004", name="Áo hoodie")],
        cfg=__import__("backend.application.director.config", fromlist=["StreamConfig"]).StreamConfig(
            transient_retry_count=1
        ),
    )
    coordinator._stats[session_id] = _SessionStats()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    decision = Decision(action="introduce_product", product_id="P004", prompt="Mở sản phẩm")

    await coordinator._maybe_speak(session_id, decision)

    assert backend.calls == 2
    assert runtime.get_session(session_id).director.state.products[0].is_introduced is True
    assert coordinator.stats(session_id)["completed_speeches"] == 1
    assert any(event["type"] == "coordinator.retry_scheduled" for event in hub.events)


@pytest.mark.asyncio
async def test_transient_turn_exhaustion_does_not_advance_state() -> None:
    backend = _FlakyCloudBackend(failures=2)
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "retry-exhausted"
    runtime.attach(
        session_id,
        [Product(id="P004", name="Áo hoodie")],
        cfg=__import__("backend.application.director.config", fromlist=["StreamConfig"]).StreamConfig(
            transient_retry_count=1
        ),
    )
    coordinator._stats[session_id] = _SessionStats()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    decision = Decision(action="introduce_product", product_id="P004", prompt="Mở sản phẩm")

    await coordinator._maybe_speak(session_id, decision)

    assert backend.calls == 2
    assert runtime.get_session(session_id).director.state.products[0].is_introduced is False
    assert coordinator.stats(session_id)["completed_speeches"] == 0


@pytest.mark.asyncio
async def test_pipeline_prepares_upcoming_turns_during_active_playback() -> None:
    backend = _BlockingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "prepared-pipeline"
    coordinator.start(
        session_id,
        [Product(id="P004", name="Áo hoodie HeyGen")],
        activated=True,
    )
    try:
        await coordinator._tick_once(session_id)
        assert await asyncio.to_thread(backend.started.wait, 1)
        deadline = asyncio.get_running_loop().time() + 1
        while coordinator.stats(session_id)["queued_decisions"] < 2:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        assert coordinator.stats(session_id)["queued_decisions"] == 2
    finally:
        backend.release.set()
        coordinator.stop(session_id)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_interrupt_invalidates_prepared_turns_and_generation() -> None:
    backend = _BlockingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "interrupt-pipeline"
    coordinator.start(
        session_id,
        [Product(id="P004", name="Áo hoodie HeyGen")],
        activated=True,
    )
    try:
        await coordinator._tick_once(session_id)
        assert await asyncio.to_thread(backend.started.wait, 1)
        before = runtime.current_generation_token(session_id)

        await coordinator.interrupt(session_id)

        stats = coordinator.stats(session_id)
        assert stats["queued_decisions"] == 0
        assert runtime.current_generation_token(session_id) != before
        assert any(turn["state"] == "cancelled_stale" for turn in stats["completed_speech_history"])
    finally:
        backend.release.set()
        coordinator.stop(session_id)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_does_not_resurrect_history_from_cancelled_preparation() -> None:
    backend = MockRenderBackend(fps=1, width=32, height=32)
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "stop-late-preparation"
    runtime.attach(session_id, [Product(id="P004", name="Áo hoodie")])
    coordinator._stats[session_id] = _SessionStats()
    coordinator._decision_queue[session_id] = __import__("collections").deque()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    coordinator._prepare_tasks[session_id] = set()
    blocker = Decision(action="speak_hook", text="Đang chờ")
    decision = Decision(action="introduce_product", product_id="P004", prompt="Mở sản phẩm")
    decision.revision_token = runtime.current_generation_token(session_id)
    coordinator._decision_queue[session_id].extend((blocker, decision))
    task = asyncio.create_task(coordinator._prepare_turn(session_id, decision))
    coordinator._prepare_tasks[session_id].add(task)
    await asyncio.sleep(0)

    coordinator.stop(session_id)
    await asyncio.gather(task, return_exceptions=True)

    assert session_id not in coordinator._completed_history


@pytest.mark.asyncio
async def test_stop_during_active_playback_does_not_resurrect_history() -> None:
    backend = _BlockingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "stop-active-playback"
    runtime.attach(session_id, [Product(id="P004", name="Áo hoodie")])
    coordinator._stats[session_id] = _SessionStats()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    decision = Decision(action="introduce_product", product_id="P004", prompt="Mở sản phẩm")

    task = asyncio.create_task(coordinator._maybe_speak(session_id, decision))
    assert await asyncio.to_thread(backend.started.wait, 1)
    coordinator.stop(session_id)
    backend.release.set()
    await task

    assert session_id not in coordinator._completed_history


@pytest.mark.asyncio
async def test_failed_preparation_removes_head_decision() -> None:
    class FailingLLM:
        name = "failing"

        def stream_chunks(self, request, *, session_id: str, utterance_id: str):
            raise ConnectionError("generation failed")
            yield

    backend = MockRenderBackend(fps=1, width=32, height=32)
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(
        runtime=runtime,
        llm=FailingLLM(),
        tts=None,
        backend=backend,
    )
    session_id = "failed-preparation"
    runtime.attach(session_id, [Product(id="P004", name="Áo hoodie")])
    coordinator._stats[session_id] = _SessionStats()
    coordinator._decision_queue[session_id] = __import__("collections").deque()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    decision = Decision(action="introduce_product", product_id="P004", prompt="Mở sản phẩm")
    decision.revision_token = runtime.current_generation_token(session_id)
    coordinator._decision_queue[session_id].append(decision)

    coordinator._activated.add(session_id)
    await coordinator._prepare_turn(session_id, decision)

    assert list(coordinator._decision_queue[session_id]) == []
    assert list(coordinator._speech_queue[session_id]) == []
    assert session_id not in coordinator._activated
    assert coordinator.stats(session_id)["completed_speech_history"][-1]["state"] == "failed"


class _VariantLLM:
    name = "variant-llm"

    def __init__(self) -> None:
        self.calls = 0

    def stream_chunks(self, request, *, session_id: str, utterance_id: str):
        from core.render.windows import TextChunk

        self.calls += 1
        yield TextChunk(
            session_id=session_id,
            utterance_id=utterance_id,
            seq=0,
            text=f"Biến thể trả lời {self.calls}.",
            is_final=True,
        )


@pytest.mark.asyncio
async def test_qna_preparation_populates_all_cache_variants() -> None:
    backend = MockRenderBackend(fps=1, width=32, height=32)
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    llm = _VariantLLM()
    coordinator = DirectorCoordinator(runtime=runtime, llm=llm, tts=None, backend=backend)
    session_id = "answer-variants"
    runtime.attach(session_id, [Product(id="P004", name="Áo hoodie")])
    coordinator._stats[session_id] = _SessionStats()
    coordinator._decision_queue[session_id] = __import__("collections").deque()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    coordinator._playback_events[session_id] = asyncio.Event()
    decision = Decision(
        action="answer_cluster",
        product_id="P004",
        prompt="Trả lời giá",
        topic="price",
        cluster_members=("Giá bao nhiêu?", "Bao nhiêu tiền?"),
        cluster_member_ids=("price-1", "price-2"),
    )
    decision.revision_token = runtime.current_generation_token(session_id)
    coordinator._decision_queue[session_id].append(decision)

    await coordinator._prepare_turn(session_id, decision)
    runtime.get_session(session_id).director.mark_spoken(decision)

    cache = runtime.get_session(session_id).director.state.answer_variants
    assert list(cache.values()) == [
        ["Biến thể trả lời 1.", "Biến thể trả lời 2.", "Biến thể trả lời 3."]
    ]


@pytest.mark.asyncio
async def test_cloud_preparation_uses_core_llm_then_verbatim_playback() -> None:
    backend = _RecordingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(
        runtime=runtime,
        llm=_VariantLLM(),
        tts=None,
        backend=backend,
    )
    session_id = "cloud-preparation"
    runtime.attach(session_id, [Product(id="P004", name="Áo hoodie")])
    coordinator._stats[session_id] = _SessionStats()
    coordinator._decision_queue[session_id] = __import__("collections").deque()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    coordinator._playback_events[session_id] = asyncio.Event()
    decision = Decision(
        action="introduce_product",
        product_id="P004",
        prompt="Mở sản phẩm",
    )
    decision.revision_token = runtime.current_generation_token(session_id)
    coordinator._decision_queue[session_id].append(decision)

    await coordinator._prepare_turn(session_id, decision)
    await coordinator._maybe_speak(session_id, decision)

    assert backend.calls == [("Biến thể trả lời 1.", False)]


@pytest.mark.asyncio
async def test_tick_keeps_prepared_next_product_transition() -> None:
    from backend.api.v1.router import build_run_plan
    from backend.application.director.comment_buffer import ChatQueue
    from backend.application.director.config import StreamConfig

    backend = _RecordingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "prepared-transition"
    products = [
        Product(id="P004", name="Áo hoodie", features=["ấm"]),
        Product(id="P001", name="Kem chống nắng", features=["nhẹ mặt"]),
    ]
    runtime.attach(
        session_id,
        products,
        cfg=StreamConfig(prepared_turn_depth=1),
        run_plan=build_run_plan([item.__dict__ for item in products]),
    )
    session = runtime.get_session(session_id)
    session.director.state.phase = __import__(
        "backend.application.director.state", fromlist=["Phase"]
    ).Phase.SELLING
    first = session.director.state.products[0]
    first.is_introduced = True
    first.stage_turn_index = len(session.director._sales_tasks("P004"))
    coordinator._queues[session_id] = ChatQueue(session_id)
    coordinator._stats[session_id] = _SessionStats()
    coordinator._decision_queue[session_id] = __import__("collections").deque()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._prepare_tasks[session_id] = set()
    coordinator._decision_locks[session_id] = asyncio.Lock()
    coordinator._playback_events[session_id] = asyncio.Event()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)
    coordinator._activated.add(session_id)
    coordinator._last_tick[session_id] = session.now()

    await coordinator._fill_prepared(session_id)
    await asyncio.gather(*coordinator._prepare_tasks[session_id])
    queued_turn_id = coordinator._speech_queue[session_id][0].turn_id
    await coordinator._tick_once(session_id)

    queued_ids = {
        decision.turn_id
        for decision in (
            *coordinator._decision_queue[session_id],
            *coordinator._speech_queue[session_id],
        )
    }
    assert queued_turn_id in queued_ids


@pytest.mark.asyncio
async def test_playback_loop_requeues_turn_while_manual_speech_holds_lock() -> None:
    backend = _RecordingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "manual-lock"
    runtime.attach(session_id, [Product(id="P004", name="Áo hoodie")])
    coordinator._stats[session_id] = _SessionStats()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._playback_events[session_id] = asyncio.Event()
    decision = Decision(
        action="introduce_product",
        product_id="P004",
        prompt="Mở sản phẩm",
        prepared_script="Kịch bản sẵn sàng",
        may_interrupt=True,
        score=10.0,
    )
    decision.revision_token = runtime.current_generation_token(session_id)
    coordinator._speech_queue[session_id].append(decision)
    assert coordinator._lock_registry.try_acquire(session_id)

    task = asyncio.create_task(coordinator._playback_loop(session_id))
    coordinator._playback_events[session_id].set()
    await asyncio.sleep(0.02)

    task.cancel()
    await task
    assert list(coordinator._speech_queue[session_id]) == [decision]


@pytest.mark.asyncio
async def test_completed_close_commits_real_closing_phase() -> None:
    from backend.api.v1.router import build_run_plan
    from backend.application.director.config import StreamConfig
    from backend.application.director.state import Phase

    backend = _RecordingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    session_id = "prepared-close"
    product = Product(id="P004", name="Áo hoodie", features=["ấm"])
    runtime.attach(
        session_id,
        [product],
        cfg=StreamConfig(prepared_turn_depth=1),
        run_plan=build_run_plan([product.__dict__]),
    )
    session = runtime.get_session(session_id)
    session.director.state.phase = Phase.SELLING
    current = session.director.state.products[0]
    current.is_introduced = True
    current.stage_turn_index = len(session.director._sales_tasks("P004"))
    coordinator._stats[session_id] = _SessionStats()
    coordinator._decision_queue[session_id] = __import__("collections").deque()
    coordinator._speech_queue[session_id] = __import__("collections").deque()
    coordinator._prepare_tasks[session_id] = set()
    coordinator._decision_locks[session_id] = asyncio.Lock()
    coordinator._playback_events[session_id] = asyncio.Event()
    coordinator._completed_history[session_id] = __import__("collections").deque(maxlen=3)

    await coordinator._fill_prepared(session_id)
    await asyncio.gather(*coordinator._prepare_tasks[session_id])
    decision = coordinator._speech_queue[session_id].popleft()
    await coordinator._maybe_speak(session_id, decision)

    assert session.director.state.phase is Phase.CLOSING
    assert session.director.state.closing_spoken is True


@pytest.mark.asyncio
async def test_cluster_snapshot_reuses_runtime_embedder_cache_and_session_config() -> None:
    backend = MockRenderBackend()
    session_id = "cluster-snapshot"
    session = _MockSession(session_id=session_id)
    session.idle_loop_frames = backend._build_idle_loop(session_id)
    backend._sessions[session_id] = session
    embedder = _CountingEmbedder()
    runtime = DirectorRuntime(backend=backend, embedder=embedder)
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    from backend.application.director.config import StreamConfig

    coordinator.start(
        session_id,
        [Product(id="P004", name="Áo hoodie HeyGen")],
        cfg=StreamConfig(selection_window_sec=60, cluster_merge_threshold=0.91),
    )
    try:
        director_session = runtime.get_session(session_id)
        director_session.director.decide = lambda comments, now: Decision(action="idle")
        now = time.time()
        coordinator.ingest(session_id, "đã quá cũ", "viewer", ts=now - 120)
        coordinator.ingest(session_id, "áo hoodie giá bao nhiêu", "viewer", ts=now)
        coordinator.ingest(session_id, "hoodie này giá bao nhiêu", "viewer", ts=now)
        await coordinator._tick_once(session_id)
        encoded_after_tick = len(embedder.encoded_batches)

        first = coordinator.cluster_snapshot(session_id)
        second = coordinator.cluster_snapshot(session_id)

        assert len(embedder.encoded_batches) == encoded_after_tick
        volatile = {"snapshot_at", "oldest_ms_ago"}
        assert {key: value for key, value in first.items() if key not in volatile} == {
            key: value for key, value in second.items() if key not in volatile
        }
        assert first["received_total"] == 3
        assert first["buffered_comments"] == 3
        assert first["active_comments"] == 2
        assert first["cluster_merge_threshold"] == 0.91
        assert first["selection_window_sec"] == 60
        assert first["embedder_name"] == "hashing-fallback"
        assert first["embedder_status"] == "degraded"
        diagnostics = coordinator.stats(session_id)
        assert diagnostics["generation_token"] == runtime.current_generation_token(session_id)
        assert diagnostics["accepted_snapshot"]["products"]
        assert diagnostics["pivot_state"]["active"] is False
        assert diagnostics["answer_cache"] == {"keys": 0, "variants": 0}
    finally:
        coordinator.stop(session_id)
        await asyncio.sleep(0)
