"""Trailing-silence trimming tests (task trailing-silence-trim).

``trim_trailing_silence`` removes the long trailing silence that edge-tts
appends to EVERY request (~0.85 s). Without trimming, each chunk boundary in
the concat pipeline becomes a ~0.85 s gap. The trim keeps a short ``keep_ms``
tail so speech is never cut flush. All PCM is synthesized (struct), no
wall-clock sleeps.
"""

from __future__ import annotations

import asyncio
import struct
import queue
from typing import Iterator

import pytest

from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from avatar.engines.mock import MockRenderBackend
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow, trim_trailing_silence
from backend.application.text_chunker import FixedChunkPolicyConfig, TextChunk
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.orchestrator import (
    StreamOrchestrator,
    StreamingControllerConfig,
)


def _pcm(*samples: int) -> bytes:
    """Pack int16 mono samples into raw PCM bytes."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _speech_ms(ms: int, sample_rate: int, value: int = 5000) -> bytes:
    return _pcm(*([value] * (sample_rate * ms // 1000)))


def _silence_ms(ms: int, sample_rate: int) -> bytes:
    return _pcm(*([0] * (sample_rate * ms // 1000)))


class _StubLLM(LLMEngine):
    """LLM stub yielding nothing (never iterated in these tests)."""

    name = "stub-llm"

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubLLM":  # pragma: no cover
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        return iter(())


def _build_orchestrator() -> tuple[StreamOrchestrator, MockRenderBackend]:
    backend = MockRenderBackend()
    backend.start(StartOptions())
    queue_ = BoundedVideoQueue(max_size=20)
    orch = StreamOrchestrator(
        llm=_StubLLM(),
        tts=object(),  # never synthesized in these tests
        backend=backend,
        queue=queue_,
        metrics=CoordinatorMetrics(),
        fixed_config=FixedChunkPolicyConfig(min_chars=4, target_chars=20, max_chars=40),
        controller_config=StreamingControllerConfig(),
    )
    orch._loop = asyncio.get_running_loop()
    return orch, backend


# ---------------------------------------------------------------------------
# trim_trailing_silence unit tests
# ---------------------------------------------------------------------------


def test_trims_trailing_silence_keeps_tail() -> None:
    pcm = _speech_ms(100, 16000) + _silence_ms(800, 16000)
    trimmed, removed_ms = trim_trailing_silence(pcm, 16000)

    assert removed_ms == 550  # 800 ms run - 250 ms kept
    assert len(trimmed) == (1600 + 4000) * 2  # 100 ms speech + 250 ms tail
    assert trimmed == _speech_ms(100, 16000) + _silence_ms(250, 16000)


def test_no_trailing_silence_returns_unchanged() -> None:
    pcm = _speech_ms(200, 16000)
    trimmed, removed_ms = trim_trailing_silence(pcm, 16000)

    assert removed_ms == 0
    assert trimmed is pcm


def test_all_silence_trims_to_keep_tail() -> None:
    pcm = _silence_ms(1000, 16000)
    trimmed, removed_ms = trim_trailing_silence(pcm, 16000)

    assert removed_ms == 750  # 1000 ms run - 250 ms kept
    assert trimmed == _silence_ms(250, 16000)


def test_short_pcm_not_trimmed() -> None:
    pcm = _speech_ms(50, 16000) + _silence_ms(300, 16000)  # 350 ms < keep + min
    trimmed, removed_ms = trim_trailing_silence(pcm, 16000)

    assert removed_ms == 0
    assert trimmed is pcm


def test_removed_ms_consistent_across_sample_rates() -> None:
    for rate in (8000, 16000):
        pcm = _speech_ms(100, rate) + _silence_ms(800, rate)
        trimmed, removed_ms = trim_trailing_silence(pcm, rate)

        assert removed_ms == 550  # ms-based, rate-independent
        # 100 ms speech + 250 ms tail, byte count scales with rate.
        assert len(trimmed) == (rate * 100 // 1000 + rate * 250 // 1000) * 2


def test_odd_byte_count_does_not_crash() -> None:
    pcm = _speech_ms(100, 16000) + _silence_ms(800, 16000) + b"\x00"
    assert len(pcm) % 2 == 1

    trimmed, removed_ms = trim_trailing_silence(pcm, 16000)

    assert removed_ms == 550
    assert len(trimmed) == (1600 + 4000) * 2


def test_empty_pcm_returns_unchanged() -> None:
    trimmed, removed_ms = trim_trailing_silence(b"", 16000)

    assert removed_ms == 0
    assert trimmed == b""


# ---------------------------------------------------------------------------
# Orchestrator integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_audio_window_trims_and_preserves_id_and_finality() -> None:
    orch, backend = _build_orchestrator()
    sid = next(iter(backend._sessions.keys()))

    captured: dict[str, AudioWindow] = {}

    async def on_window(window: AudioWindow) -> None:
        captured["window"] = window

    orch._audio_window_callback = on_window

    pcm = _speech_ms(100, 16000) + _silence_ms(800, 16000)
    window = AudioWindow(
        session_id=sid,
        utterance_id="u1",
        seq=0,
        sample_rate=16000,
        duration_ms=900,
        pcm=pcm,
        is_final=False,
    )

    # _deliver_audio_window is a SYNC worker-thread function: it blocks on
    # run_coroutine_threadsafe(callback, loop).result(). Calling it from the
    # event loop thread would deadlock, so drive it from a worker thread the
    # way the orchestrator does (asyncio.to_thread).
    await asyncio.to_thread(
        orch._deliver_audio_window,
        window,
        is_final=True,
        text_span="hello",
        session_id=sid,
        bridge=queue.Queue(),
    )

    delivered = captured["window"]
    assert delivered.id == window.id
    assert delivered.is_final is True
    assert delivered.text_span == "hello"
    assert delivered.duration_ms == 350  # 900 - 550 removed
    assert len(delivered.pcm) == (1600 + 4000) * 2


@pytest.mark.asyncio
async def test_deliver_audio_window_skips_deferred_path() -> None:
    orch, backend = _build_orchestrator()
    sid = next(iter(backend._sessions.keys()))

    captured: dict[str, AudioWindow] = {}

    async def on_window(window: AudioWindow) -> None:
        captured["window"] = window

    orch._audio_window_callback = on_window

    window = AudioWindow(
        session_id=sid,
        utterance_id="u1",
        seq=0,
        sample_rate=16000,
        duration_ms=900,
        audio_path="x.wav",  # no PCM: deferred path must be left untouched
        is_final=True,
    )

    # _deliver_audio_window is a SYNC worker-thread function: it blocks on
    # run_coroutine_threadsafe(callback, loop).result(). Calling it from the
    # event loop thread would deadlock, so drive it from a worker thread the
    # way the orchestrator does (asyncio.to_thread).
    await asyncio.to_thread(
        orch._deliver_audio_window,
        window,
        is_final=True,
        text_span="hello",
        session_id=sid,
        bridge=queue.Queue(),
    )

    delivered = captured["window"]
    assert delivered.id == window.id
    assert delivered.duration_ms == 900
    assert delivered.audio_path == "x.wav"
    assert delivered.pcm is None
