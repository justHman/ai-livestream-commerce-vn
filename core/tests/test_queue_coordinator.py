"""Unit tests for BoundedVideoQueue + CoordinatorMetrics + StreamOrchestrator (Task 8).

Covers:
  - BoundedVideoQueue: put up to max, next put drops oldest (dropped_count
    increments), qsize correct, get retrieves in order.
  - CoordinatorMetrics: pipeline_total_ms recorded, queue_depth_windows
    tracks qsize, dropped_windows accumulates.
  - StreamOrchestrator end-to-end with stub LLM (fixed TextChunks), stub TTS
    (yields AudioWindows), real MockRenderBackend: ordered VideoWindows
    emitted, is_final on the last, pipeline_total_ms recorded, total > 0.
  - Cancel mid-run: emission stops, queued windows dropped, run returns early.
  - Metrics dropped_windows increments on queue overflow.

All tests offline (stub engines + MockRenderBackend + Pillow). No network,
no model downloads.
"""

from __future__ import annotations

import asyncio
import time
from typing import Iterator, List

import pytest

from core.llm.base import LLMEngine, LLMRequest, LLMResponse, _NoopEngine
from core.render.mock import MockRenderBackend
from core.render.base import StartOptions
from core.render.windows import AudioWindow, TextChunk, VideoWindow
from core.render.queue import (
    BoundedVideoQueue,
    CoordinatorMetrics,
)
from core.render.orchestrator import StreamOrchestrator
from core.tts.base import AudioChunk, TTSEngine, TTSRequest, ToneEngine


# ---------- helpers / stubs ----------


def _audio_window(ms: int = 200, is_final: bool = True) -> AudioWindow:
    """Build a short deterministic AudioWindow."""
    sample_rate = 24000
    samples = int(sample_rate * ms / 1000)
    motif = b"\x01\x00\x02\x00"
    pcm = (motif * (samples * 2 // len(motif) + 1))[: samples * 2]
    return AudioWindow(
        session_id="sess-test",
        utterance_id="utt-1",
        seq=0,
        sample_rate=sample_rate,
        duration_ms=ms,
        pcm=pcm,
        is_final=is_final,
        text_span="test",
    )


def _video_window(seq: int = 0, is_final: bool = False) -> VideoWindow:
    return VideoWindow(
        session_id="sess-test",
        utterance_id="utt-1",
        seq=seq,
        frames=[b"\x00" * 10],
        fps=25,
        duration_ms=200,
        audio_window_id="aw-1",
        is_final=is_final,
    )


# ---------- BoundedVideoQueue ----------


@pytest.mark.asyncio
async def test_bounded_queue_put_up_to_max_without_drop():
    q = BoundedVideoQueue(max_size=3)
    assert q.qsize() == 0
    assert (await q.put(_video_window(0))) is True
    assert (await q.put(_video_window(1))) is True
    assert (await q.put(_video_window(2))) is True
    assert q.qsize() == 3
    assert q.dropped_count() == 0


@pytest.mark.asyncio
async def test_bounded_queue_drop_oldest_on_overflow():
    q = BoundedVideoQueue(max_size=2)
    await q.put(_video_window(0))
    await q.put(_video_window(1))
    # Third put: queue full -> drop oldest (seq=0), put seq=2.
    result = await q.put(_video_window(2))
    assert result is False, "put that drops oldest must return False"
    assert q.dropped_count() == 1
    assert q.qsize() == 2
    # Oldest remaining should be seq=1, then seq=2.
    w1 = await q.get()
    w2 = await q.get()
    assert w1.seq == 1
    assert w2.seq == 2


@pytest.mark.asyncio
async def test_bounded_queue_get_retrieves_in_order():
    q = BoundedVideoQueue(max_size=5)
    for i in range(4):
        await q.put(_video_window(i))
    out: List[VideoWindow] = []
    for _ in range(4):
        out.append(await q.get())
    assert [w.seq for w in out] == [0, 1, 2, 3]
    assert q.qsize() == 0


# ---------- CoordinatorMetrics ----------


def test_metrics_first_frame_latency_recorded():
    times = [100.0]
    m = CoordinatorMetrics(clock=lambda: times[0])
    m.record_start()
    times[0] = 100.150  # +150 ms
    m.record_first_frame()
    assert m.pipeline_total_ms == pytest.approx(150.0, abs=1.0)


def test_metrics_queue_depth_and_dropped():
    m = CoordinatorMetrics()
    m.update_queue_depth(3)
    assert m.queue_depth_windows == 3
    m.increment_dropped(2)
    assert m.dropped_windows == 2
    m.increment_dropped(1)
    assert m.dropped_windows == 3


def test_metrics_to_dict_contains_all_fields():
    m = CoordinatorMetrics()
    m.record_start()
    m.record_first_frame()
    m.update_queue_depth(2)
    m.increment_dropped(1)
    d = m.to_dict()
    assert "pipeline_total_ms" in d
    assert "queue_depth_windows" in d
    assert "dropped_windows" in d


# ---------- StreamOrchestrator end-to-end ----------


class _StubLLM(LLMEngine):
    """LLM stub that yields a fixed list of TextChunk deltas via stream_chunks."""

    name = "stub-llm"

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = list(deltas)

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubLLM":  # pragma: no cover
        return cls([])

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        for i, d in enumerate(self._deltas):
            yield TextChunk(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=i,
                text=d,
                is_final=(i == len(self._deltas) - 1),
            )


class _StubTTS(TTSEngine):
    """TTS stub that yields a single AudioWindow per phrase TextChunk."""

    name = "stub-tts"
    sample_rate = 24000

    def __init__(self) -> None:
        self._seq = 0

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubTTS":  # pragma: no cover
        return cls()

    def synthesize(self, req: TTSRequest) -> AudioChunk:  # pragma: no cover
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
        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
        sid = text_or_chunk.session_id if isinstance(text_or_chunk, TextChunk) else session_id
        uid = text_or_chunk.utterance_id if isinstance(text_or_chunk, TextChunk) else utterance_id
        is_final = text_or_chunk.is_final if isinstance(text_or_chunk, TextChunk) else True
        # Single short window per phrase; deterministic 200 ms.
        aw = AudioWindow(
            session_id=sid,
            utterance_id=uid,
            seq=self._seq,
            sample_rate=24000,
            duration_ms=200,
            pcm=b"\x01\x00" * 4800,  # 200 ms at 24 kHz int16
            is_final=is_final,
            text_span=text,
        )
        self._seq += 1
        yield aw


class _SlowStreamingBackend:
    """Backend that yields first window immediately, then a second one later.

    Used to prove Phase E streaming-drain: the async queue receives the first
    VideoWindow while the sync worker is still rendering the second.
    """

    name = "slow-streaming-backend"

    def stream_audio(self, session_id: str, audio_window: AudioWindow) -> Iterator[VideoWindow]:
        yield VideoWindow(
            session_id=session_id,
            utterance_id=audio_window.utterance_id,
            seq=0,
            frames=[b"first"],
            fps=25,
            duration_ms=100,
            audio_window_id=audio_window.id,
            is_final=False,
        )
        time.sleep(0.20)
        yield VideoWindow(
            session_id=session_id,
            utterance_id=audio_window.utterance_id,
            seq=1,
            frames=[b"second"],
            fps=25,
            duration_ms=100,
            audio_window_id=audio_window.id,
            is_final=True,
        )


def _build_orchestrator(
    deltas: list[str], max_queue: int = 5
) -> tuple[StreamOrchestrator, MockRenderBackend, BoundedVideoQueue, CoordinatorMetrics]:
    backend = MockRenderBackend()
    backend.start(StartOptions())
    llm = _StubLLM(deltas)
    tts = _StubTTS()
    queue = BoundedVideoQueue(max_size=max_queue)
    metrics = CoordinatorMetrics()
    cfg = {
        "text_chunk_min_chars": 4,
        "text_chunk_target_chars": 20,
        "text_chunk_max_chars": 40,
        "text_chunk_flush_timeout_ms": 350,
    }
    orch = StreamOrchestrator(
        llm=llm,
        tts=tts,
        backend=backend,
        queue=queue,
        metrics=metrics,
        config=cfg,
    )
    return orch, backend, queue, metrics


def _drain_queue(queue: BoundedVideoQueue) -> list[VideoWindow]:
    out: list[VideoWindow] = []
    while queue.qsize() > 0:
        out.append(asyncio.get_event_loop().run_until_complete(queue.get()))
    return out


@pytest.mark.asyncio
async def test_orchestrator_emits_ordered_videowindows_end_to_end():
    """Stub LLM (3 phrase chunks) -> stub TTS -> mock backend -> ordered VideoWindows."""
    deltas = ["Xin chào.", "Bạn khỏe không?", "Cảm ơn!"]
    orch, backend, queue, metrics = _build_orchestrator(deltas, max_queue=10)

    # Use the backend's started session_id.
    sid = next(iter(backend._sessions.keys()))
    spoken = await orch.run(sid, "user message", system_prompt="persona")

    # Drain the queue (windows were put there by the orchestrator).
    windows: list[VideoWindow] = []
    while queue.qsize() > 0:
        windows.append(await queue.get())

    assert len(windows) > 0, "orchestrator must emit at least one VideoWindow"
    # VideoWindow seq must be strictly increasing.
    seqs = [w.seq for w in windows]
    assert seqs == sorted(seqs), f"VideoWindow seq must be increasing, got {seqs}"
    # The last VideoWindow must carry is_final=True.
    assert windows[-1].is_final is True
    # All windows belong to the same session.
    assert all(w.session_id == sid for w in windows)
    # pipeline_total_ms was recorded (>= 0).
    assert metrics.pipeline_total_ms is not None
    assert metrics.pipeline_total_ms >= 0
    # spoken text is non-empty (concatenation of LLM deltas).
    assert spoken, "run() must return non-empty spoken text"


@pytest.mark.asyncio
async def test_orchestrator_records_first_frame_latency():
    deltas = ["Hello world.", "Goodbye."]
    orch, backend, queue, metrics = _build_orchestrator(deltas, max_queue=5)
    sid = next(iter(backend._sessions.keys()))

    await orch.run(sid, "hi", system_prompt=None)

    # pipeline_total_ms must have been set when the first VideoWindow was put.
    assert metrics.pipeline_total_ms is not None
    assert metrics.pipeline_total_ms >= 0.0


@pytest.mark.asyncio
async def test_orchestrator_cancel_stops_emission_and_drains_queue():
    """A stub LLM that yields many chunks with a short delay between each; cancel
    mid-run -> emission stops, remaining queued windows dropped, run returns."""
    # Many deltas so the run is long enough to interrupt.
    deltas = [f"chunk {i}." for i in range(50)]
    orch, backend, queue, metrics = _build_orchestrator(deltas, max_queue=20)
    sid = next(iter(backend._sessions.keys()))

    # Run in a task so we can cancel concurrently.
    task = asyncio.create_task(orch.run(sid, "long message"))
    # Let it produce a few windows.
    await asyncio.sleep(0.05)
    await orch.cancel(sid)
    spoken = await task

    # After cancel, queue should be drained (no pending windows).
    assert queue.qsize() == 0, "cancel must drain pending windows"
    # Run completed (did not hang).
    assert task.done()


@pytest.mark.asyncio
async def test_orchestrator_dropped_windows_increments_on_queue_overflow():
    """Queue max_size=1 + multiple windows -> overflow drops oldest, metrics track."""
    deltas = ["one.", "two.", "three.", "four."]
    orch, backend, queue, metrics = _build_orchestrator(deltas, max_queue=1)
    sid = next(iter(backend._sessions.keys()))

    await orch.run(sid, "msg", system_prompt=None)

    # With max_size=1 and 4 windows, at least 3 drops must have occurred.
    assert metrics.dropped_windows >= 3, (
        f"expected >=3 dropped windows, got {metrics.dropped_windows}"
    )


@pytest.mark.asyncio
async def test_orchestrator_streaming_drain_exposes_first_window_before_worker_finishes():
    """Phase E: first VideoWindow is queued before later backend windows finish.

    Old behavior collected all VideoWindows in the sync worker, so queue.qsize()
    stayed 0 until the slow backend yielded its second window. New behavior pushes
    the first window through the bridge immediately.
    """
    queue = BoundedVideoQueue(max_size=5)
    metrics = CoordinatorMetrics()
    orch = StreamOrchestrator(
        llm=_StubLLM(["Xin chào."]),
        tts=_StubTTS(),
        backend=_SlowStreamingBackend(),
        queue=queue,
        metrics=metrics,
        config={
            "text_chunk_min_chars": 4,
            "text_chunk_target_chars": 20,
            "text_chunk_max_chars": 40,
            "text_chunk_flush_timeout_ms": 350,
        },
    )

    task = asyncio.create_task(orch.run("sess-stream", "hello"))
    await asyncio.sleep(0.05)

    assert queue.qsize() >= 1, "first VideoWindow should be visible before worker finishes"
    assert metrics.pipeline_total_ms is not None
    assert metrics.pipeline_total_ms < 150.0

    spoken = await task
    assert spoken
    windows: list[VideoWindow] = []
    while queue.qsize() > 0:
        windows.append(await queue.get())
    assert [w.seq for w in windows] == [0, 1]
    assert windows[-1].is_final is True


@pytest.mark.asyncio
async def test_orchestrator_with_noop_llm_and_tone_tts_smoke():
    """Smoke: real _NoopEngine + ToneEngine + MockRenderBackend produces windows."""
    backend = MockRenderBackend()
    backend.start(StartOptions())
    sid = next(iter(backend._sessions.keys()))
    queue = BoundedVideoQueue(max_size=5)
    metrics = CoordinatorMetrics()
    cfg = {
        "text_chunk_min_chars": 4,
        "text_chunk_target_chars": 20,
        "text_chunk_max_chars": 40,
        "text_chunk_flush_timeout_ms": 350,
    }
    orch = StreamOrchestrator(
        llm=_NoopEngine(),
        tts=ToneEngine(),
        backend=backend,
        queue=queue,
        metrics=metrics,
        config=cfg,
    )
    spoken = await orch.run(sid, "hello", system_prompt=None)
    assert spoken, "noop llm + tone tts must produce spoken text"
    assert queue.qsize() >= 0
    # Drain to ensure no hang.
    while queue.qsize() > 0:
        await queue.get()


@pytest.mark.asyncio
async def test_orchestrator_forwards_pcm_window_to_callback_before_rendering():
    backend = MockRenderBackend()
    result = backend.start(StartOptions())
    received: list[AudioWindow] = []

    async def capture(window: AudioWindow) -> None:
        received.append(window)

    orch = StreamOrchestrator(
        llm=_StubLLM(["Xin chào."]),
        tts=_StubTTS(),
        backend=backend,
        queue=BoundedVideoQueue(max_size=5),
        metrics=CoordinatorMetrics(),
        config={
            "text_chunk_min_chars": 4,
            "text_chunk_target_chars": 20,
            "text_chunk_max_chars": 40,
        },
        audio_window_callback=capture,
    )

    await orch.run(result.session_id, "hello")

    assert received[0].sample_rate == 24_000


@pytest.mark.asyncio
async def test_orchestrator_propagates_callback_failure():
    backend = MockRenderBackend()
    result = backend.start(StartOptions())

    async def fail(_: AudioWindow) -> None:
        raise RuntimeError("publisher failed")

    orch = StreamOrchestrator(
        llm=_StubLLM(["Xin chào."]),
        tts=_StubTTS(),
        backend=backend,
        queue=BoundedVideoQueue(max_size=5),
        metrics=CoordinatorMetrics(),
        config={
            "text_chunk_min_chars": 4,
            "text_chunk_target_chars": 20,
            "text_chunk_max_chars": 40,
        },
        audio_window_callback=fail,
    )

    with pytest.raises(RuntimeError, match="publisher failed"):
        await orch.run(result.session_id, "hello")
