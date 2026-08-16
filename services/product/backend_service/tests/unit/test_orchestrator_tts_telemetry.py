"""End-to-end orchestrator TTS-timing telemetry tests (task 7.2).

A deterministic fake TTS (no wall-clock sleeps) yields known-duration
windows; the orchestrator's ``render_phrase`` times the real lazy stream
consumption and records both synthesis latency and generated audio duration.
"""

from __future__ import annotations

import asyncio
from typing import Iterator

import pytest

from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from avatar.engines.mock import MockRenderBackend
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow, VideoWindow
from backend.application.text_chunker import ChunkPolicy, FixedChunkPolicyConfig, TextChunk
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.orchestrator import (
    StreamOrchestrator,
    StreamingControllerConfig,
)
from backend.application.text_chunker.telemetry import TelemetryCollector


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
    """TTS stub: one 200 ms window per call, deterministic, no sleeps."""

    name = "fixed-tts"
    sample_rate = 24000

    def __init__(self) -> None:
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
            duration_ms=200,
            pcm=b"\x01\x00" * 4800,  # 200 ms at 24 kHz int16
            is_final=True,
            text_span=(
                text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
            ),
        )
        self._seq += 1
        yield window


def _build_orchestrator(
    deltas: list[str],
    telemetry: TelemetryCollector,
) -> tuple[StreamOrchestrator, MockRenderBackend, BoundedVideoQueue]:
    backend = MockRenderBackend()
    backend.start(StartOptions())
    queue = BoundedVideoQueue(max_size=20)
    metrics = CoordinatorMetrics()
    orch = StreamOrchestrator(
        llm=_StubLLM(deltas),
        tts=_FixedTTS(),
        backend=backend,
        queue=queue,
        metrics=metrics,
        fixed_config=FixedChunkPolicyConfig(min_chars=4, target_chars=20, max_chars=40),
        controller_config=StreamingControllerConfig(flush_timeout_ms=50),
        # Fixed-policy telemetry assertions (punctuation reason, policy tag):
        # the fixed policy is the EXPLICIT rollback — the runtime default is
        # adaptive_vi (P1-02), which stamps sentence/paragraph reasons.
        chunk_policy=ChunkPolicy.FIXED,
        telemetry=telemetry,
    )
    return orch, backend, queue


async def _drain(queue: BoundedVideoQueue) -> list[VideoWindow]:
    windows: list[VideoWindow] = []
    while queue.qsize() > 0:
        windows.append(await queue.get())
    return windows


@pytest.mark.asyncio
async def test_orchestrator_records_chunk_and_tts_telemetry() -> None:
    telemetry = TelemetryCollector()
    orch, backend, queue = _build_orchestrator(
        ["Xin chào bạn. Hôm nay giảm giá 50%,", " nhanh tay"],
        telemetry,
    )
    sid = next(iter(backend._sessions.keys()))

    spoken = await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)
    await _drain(queue)

    assert spoken
    records = telemetry.records
    assert records, "chunk telemetry must flow from the orchestrator's chunker"
    assert records[0].seq == 0
    assert records[0].decision_reason == "punctuation"
    assert records[0].policy == "fixed"
    assert records[-1].is_final is True
    hints = telemetry.to_runtime_hints()
    assert hints.tts_first_audio_ewma_ms is not None and hints.tts_first_audio_ewma_ms > 0.0
    assert hints.tts_rtf_ewma is not None and hints.tts_rtf_ewma > 0.0
    assert "Xin chào bạn" not in repr(records)


@pytest.mark.asyncio
async def test_orchestrator_without_collector_stays_neutral() -> None:
    orch, backend, queue = _build_orchestrator(["Xin chào bạn."], None)
    sid = next(iter(backend._sessions.keys()))

    spoken = await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)
    await _drain(queue)

    assert spoken
