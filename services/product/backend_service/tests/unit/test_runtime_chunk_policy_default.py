"""P1-02: adaptive_vi is the runtime default at every production construction seam.

The calibration gate passed (task 8.9) — ``AdaptiveViPolicyConfig`` constants
are the calibrated defaults and ``adaptive_vi`` is the intended runtime policy.
This file proves the RUNTIME paths select it without any explicit policy being
passed: ``StreamOrchestrator`` (both verbatim and streaming paths),
``PlaybackWorker.chunker``, and the approved-script handoff through the same
production constructor.

Discriminating input: "Sản phẩm này giá rất tốt ạ. Mua ngay hôm nay được giảm
giá thêm mười phần trăm." — the adaptive policy commits the earliest strong
boundary (the "." after "ạ") stamped ``sentence``; the fixed policy stamps
``punctuation`` for the same text. So asserting adaptive reasons on the default
path pins the runtime default without touching the explicit-fixed rollback
tests.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from avatar.engines.mock import MockRenderBackend
from backend.application.playback_worker import PlaybackWorker, PlaybackWorkerConfig
from backend.application.render.engines_base import StartOptions
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.windows import AudioWindow
from backend.application.text_chunker import ChunkPolicy, FixedChunkPolicyConfig, TextChunk
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from tts.engines.base import ToneEngine

VI_TEXT = "Sản phẩm này giá rất tốt ạ. Mua ngay hôm nay được giảm giá thêm mười phần trăm."


class _StubLLM(LLMEngine):
    """LLM stub yielding the full VI text as one final delta."""

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


class _RecordingTTS(ToneEngine):
    """ToneEngine subclass recording every received TextChunk input."""

    def __init__(self) -> None:
        super().__init__()
        self.received_inputs: list[TextChunk] = []

    def stream_audio(
        self, text_or_chunk, *, session_id="", utterance_id="", **kwargs
    ) -> Iterator[AudioWindow]:
        if isinstance(text_or_chunk, TextChunk):
            self.received_inputs.append(text_or_chunk)
        yield from super().stream_audio(
            text_or_chunk, session_id=session_id, utterance_id=utterance_id, **kwargs
        )


def _new_orchestrator(tts: _RecordingTTS, deltas: list[str]):
    """Build a production StreamOrchestrator WITHOUT passing any policy."""
    from backend.application.render.orchestrator import (
        StreamOrchestrator,
        StreamingControllerConfig,
    )

    backend = MockRenderBackend()
    backend.start(StartOptions())
    queue = BoundedVideoQueue(max_size=20)
    metrics = CoordinatorMetrics()
    orch = StreamOrchestrator(
        llm=_StubLLM(deltas),
        tts=tts,
        backend=backend,
        queue=queue,
        metrics=metrics,
        fixed_config=FixedChunkPolicyConfig(min_chars=4, target_chars=20, max_chars=40),
        controller_config=StreamingControllerConfig(flush_timeout_ms=50),
    )
    sid = next(iter(backend._sessions.keys()))
    return orch, backend, queue, sid


async def _drain(queue: BoundedVideoQueue) -> None:
    while queue.qsize() > 0:
        await queue.get()


@pytest.mark.asyncio
async def test_speak_verbatim_default_policy_is_adaptive() -> None:
    """The verbatim full-script path defaults to adaptive segmentation.

    A strong sentence boundary stamps ``sentence`` under adaptive_vi; the same
    text stamps ``punctuation`` under the fixed rollback. No policy is passed —
    the runtime default must be adaptive.
    """
    tts = _RecordingTTS()
    orch, backend, queue, sid = _new_orchestrator(tts, [])
    assert orch.chunk_policy == ChunkPolicy.ADAPTIVE_VI, (
        f"the orchestrator runtime default must be adaptive_vi, got {orch.chunk_policy!r}"
    )

    await orch.speak_verbatim(sid, VI_TEXT)
    await _drain(queue)

    reasons = [chunk.decision_reason for chunk in tts.received_inputs]
    assert reasons, "verbatim path must emit chunks"
    assert any(reason in ("paragraph", "sentence", "clause") for reason in reasons), (
        f"default path must show adaptive reasons, got {reasons}"
    )
    assert "".join(chunk.text for chunk in tts.received_inputs) == VI_TEXT


@pytest.mark.asyncio
async def test_run_streaming_default_policy_is_adaptive() -> None:
    """The streaming LLM path defaults to adaptive segmentation too."""
    tts = _RecordingTTS()
    orch, backend, queue, sid = _new_orchestrator(tts, [VI_TEXT])

    await orch.run(sid, "user message")
    await _drain(queue)

    reasons = [chunk.decision_reason for chunk in tts.received_inputs]
    assert reasons, "streaming path must emit chunks"
    assert any(reason in ("paragraph", "sentence", "clause") for reason in reasons), (
        f"default streaming path must show adaptive reasons, got {reasons}"
    )


def test_playback_worker_chunker_default_policy_is_adaptive() -> None:
    """``PlaybackWorker.chunker`` selects adaptive segmentation by default."""
    worker = PlaybackWorker()  # default config — no explicit policy
    chunker = worker.chunker("sess-1", "utt-1")
    assert chunker.policy == ChunkPolicy.ADAPTIVE_VI, (
        f"PlaybackWorker.chunker must default to adaptive_vi, got {chunker.policy!r}"
    )

    chunks = chunker.feed(VI_TEXT) + chunker.finalize()
    reasons = [chunk.decision_reason for chunk in chunks]
    assert any(reason in ("paragraph", "sentence", "clause") for reason in reasons), (
        f"default chunker must segment adaptively, got {reasons}"
    )


def test_playback_worker_config_policy_default_is_adaptive() -> None:
    """``PlaybackWorkerConfig`` carries the typed single-policy seam."""
    assert PlaybackWorkerConfig().chunk_policy == ChunkPolicy.ADAPTIVE_VI


@pytest.mark.asyncio
async def test_approved_script_handoff_default_policy_is_adaptive() -> None:
    """The approved-script path (``speak_verbatim``) uses adaptive by default.

    ``speak_approved_script`` hands the resolved text to the SAME production
    ``StreamOrchestrator`` — proving the Change B path inherits the Change A
    adaptive default through the constructor seam.
    """
    from backend.application.script_authoring.runtime_handoff import (
        ResolvedApprovedScript,
        speak_approved_script,
    )

    script = ResolvedApprovedScript(
        product_id="P001", approved_version_id="v-1", spoken_text=VI_TEXT
    )
    tts = _RecordingTTS()
    orch, backend, queue, sid = _new_orchestrator(tts, [])

    spoken = await speak_approved_script(orch, session_id=sid, script=script)
    await _drain(queue)

    assert spoken == VI_TEXT
    reasons = [chunk.decision_reason for chunk in tts.received_inputs]
    assert any(reason in ("paragraph", "sentence", "clause") for reason in reasons), (
        f"approved-script default path must show adaptive reasons, got {reasons}"
    )


@pytest.mark.asyncio
async def test_explicit_fixed_policy_rollback_still_available() -> None:
    """Explicit ``chunk_policy=fixed`` keeps the fixed rollback deterministic.

    The same discriminating text stamps ``punctuation`` under the explicit
    fixed rollback — the default-path adaptive assertion cannot be satisfied
    by accident.
    """
    from backend.application.render.orchestrator import (
        StreamOrchestrator,
        StreamingControllerConfig,
    )

    backend = MockRenderBackend()
    backend.start(StartOptions())
    tts = _RecordingTTS()
    video_queue = BoundedVideoQueue(max_size=20)
    orch = StreamOrchestrator(
        llm=_StubLLM([]),
        tts=tts,
        backend=backend,
        queue=video_queue,
        metrics=CoordinatorMetrics(),
        fixed_config=FixedChunkPolicyConfig(min_chars=4, target_chars=20, max_chars=40),
        controller_config=StreamingControllerConfig(flush_timeout_ms=50),
        chunk_policy=ChunkPolicy.FIXED,
    )
    sid = next(iter(backend._sessions.keys()))

    await orch.speak_verbatim(sid, VI_TEXT)
    await _drain(video_queue)

    assert orch.chunk_policy == ChunkPolicy.FIXED
    reasons = [chunk.decision_reason for chunk in tts.received_inputs]
    assert "sentence" not in reasons, (
        f"fixed rollback must not show adaptive reasons, got {reasons}"
    )
    assert reasons[0] == "punctuation", (
        f"fixed rollback must stamp punctuation on the strong boundary, got {reasons}"
    )
