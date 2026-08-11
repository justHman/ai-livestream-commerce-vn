"""Playback-buffer telemetry tests (task 7.3).

The orchestration boundary records an estimated playback depth (queued-but-
undelivered video, ms) into the content-free collector; missing telemetry
must degrade to neutral None hints. Deterministic only: fake clocks, no
wall-clock sleeps beyond ``asyncio.wait_for`` timeouts.
"""

from __future__ import annotations

import asyncio
import math
from typing import Iterator

import pytest

from avatar.engines.mock import MockRenderBackend
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from backend.application.render.engines_base import StartOptions
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.windows import AudioWindow
from backend.application.text_chunker import FixedChunkPolicyConfig, TextChunk
from backend.application.render.orchestrator import (
    StreamOrchestrator,
    StreamingControllerConfig,
)
from backend.application.text_chunker.telemetry import BoundedEwma, TelemetryCollector
from backend.application.text_chunker.types import RuntimeHints


class _StubLLM(LLMEngine):
    """LLM stub yielding fixed deltas; last delta is final."""

    name = "stub-llm"

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = list(deltas)

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubLLM":  # pragma: no cover
        return cls([])

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        for i, delta in enumerate(self._deltas):
            yield TextChunk(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=i,
                text=delta,
                is_final=(i == len(self._deltas) - 1),
            )


class _FixedTTS:
    """TTS stub: one window of ``duration_ms`` per call, deterministic."""

    name = "fixed-tts"
    sample_rate = 24000

    def __init__(self, duration_ms: int = 200) -> None:
        self._duration_ms = duration_ms
        self._seq = 0

    @classmethod
    def from_config(cls, cfg: dict) -> "_FixedTTS":  # pragma: no cover
        return cls()

    def synthesize(self, req) -> object:  # pragma: no cover
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
        sid = text_or_chunk.session_id if isinstance(text_or_chunk, TextChunk) else session_id
        uid = text_or_chunk.utterance_id if isinstance(text_or_chunk, TextChunk) else utterance_id
        window = AudioWindow(
            session_id=sid,
            utterance_id=uid,
            seq=self._seq,
            sample_rate=24000,
            duration_ms=self._duration_ms,
            pcm=b"\x01\x00" * (2 * self._duration_ms * 24),  # 24 kHz int16
            is_final=True,
            text_span=(
                text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
            ),
        )
        self._seq += 1
        yield window


def _build_orchestrator(
    deltas: list[str],
    telemetry: TelemetryCollector | None,
) -> tuple[StreamOrchestrator, MockRenderBackend, BoundedVideoQueue]:
    backend = MockRenderBackend()
    backend.start(StartOptions())
    queue = BoundedVideoQueue(max_size=20)
    metrics = CoordinatorMetrics()
    orch = StreamOrchestrator(
        llm=_StubLLM(deltas),
        tts=_FixedTTS(duration_ms=200),
        backend=backend,
        queue=queue,
        metrics=metrics,
        fixed_config=FixedChunkPolicyConfig(min_chars=4, target_chars=20, max_chars=40),
        controller_config=StreamingControllerConfig(flush_timeout_ms=50),
        telemetry=telemetry,
    )
    return orch, backend, queue


async def _drain(queue: BoundedVideoQueue) -> None:
    while queue.qsize() > 0:
        await queue.get()


# ---------------------------------------------------------------------------
# Collector: EWMA + neutral degradation
# ---------------------------------------------------------------------------


def test_playback_ewma_is_bounded_window() -> None:
    collector = TelemetryCollector(ewma_alpha=0.3, ewma_window=8)
    for depth in range(20):
        collector.record_playback_buffer(float(depth * 100))
    assert collector._playback.window_size == 8  # private probe: fixed window
    assert len(collector._playback._values) <= 8  # private probe: bounded


def test_playback_ewma_ignores_non_finite() -> None:
    collector = TelemetryCollector()
    collector.record_playback_buffer(float("nan"))
    collector.record_playback_buffer(float("inf"))
    assert collector.to_runtime_hints().playback_buffer_ms is None
    collector.record_playback_buffer(3000.0)
    collector.record_playback_buffer(float("nan"))
    assert collector.to_runtime_hints().playback_buffer_ms == pytest.approx(3000.0)


def test_playback_hint_neutral_before_any_record() -> None:
    collector = TelemetryCollector()
    hints = collector.to_runtime_hints()
    assert hints == RuntimeHints()
    assert hints.playback_buffer_ms is None


def test_playback_hint_finite_after_record() -> None:
    collector = TelemetryCollector()
    collector.record_playback_buffer(5000.0)
    assert collector.to_runtime_hints().playback_buffer_ms == pytest.approx(5000.0)


def test_playback_hint_matches_reference_ewma() -> None:
    collector = TelemetryCollector(ewma_alpha=0.3, ewma_window=8)
    reference = BoundedEwma(alpha=0.3, window=8)
    samples = [1000.0, 2000.0, 4000.0, 6000.0, 9000.0]
    for depth in samples:
        collector.record_playback_buffer(depth)
        reference.update(depth)
    assert collector.to_runtime_hints().playback_buffer_ms == pytest.approx(reference.value)


def test_repr_contains_no_text() -> None:
    collector = TelemetryCollector()
    collector.record_playback_buffer(3000.0)
    assert "Xin chào" not in repr(collector)
    assert repr(collector).isprintable()


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_records_playback_depth() -> None:
    telemetry = TelemetryCollector()
    orch, backend, queue = _build_orchestrator(["Xin chào bạn.", " Nhanh tay nào"], telemetry)
    sid = next(iter(backend._sessions.keys()))

    await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)
    await _drain(queue)

    hints = telemetry.to_runtime_hints()
    assert hints.playback_buffer_ms is not None
    assert math.isfinite(hints.playback_buffer_ms)
    assert hints.playback_buffer_ms > 0.0


@pytest.mark.asyncio
async def test_speak_verbatim_records_playback_depth() -> None:
    telemetry = TelemetryCollector()
    orch, backend, queue = _build_orchestrator([], telemetry)
    sid = next(iter(backend._sessions.keys()))

    await asyncio.wait_for(orch.speak_verbatim(sid, "Xin chào bạn."), timeout=5.0)
    await _drain(queue)

    assert telemetry.to_runtime_hints().playback_buffer_ms is not None


@pytest.mark.asyncio
async def test_without_collector_stays_neutral() -> None:
    orch, backend, queue = _build_orchestrator(["Xin chào bạn."], None)
    sid = next(iter(backend._sessions.keys()))

    spoken = await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)
    await _drain(queue)

    assert spoken
