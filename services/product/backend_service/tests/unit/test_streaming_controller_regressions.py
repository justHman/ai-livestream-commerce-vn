"""Regression tests for OpenSpec adaptive-speech-text-chunking.

Covers task 1.7 (stalled-LLM deadline — fixed via the bounded stream
controller, now PASS), task 1.8 (E2E finality — INTENDED RED, normalization
pending), and task 4.x (bounded LLM stream controller: producer-thread
ownership, typed events, backpressure, cooperative cleanup, deadline).

Stub engines mirror test_playback_queue.py. The TTS stub models production
per-call finality (tts/engines/base.py): the last window of every synthesis
call is is_final=True, input finality ignored. The orchestrator passes plain
strings into tts.stream_audio, so no finality survives the boundary — the
orchestrator must normalize per-call finals into exactly one utterance-level
final.

Intended-failure map (observed 2026-08-10 at c6398a9): the five task-1.8
tests are INTENDED RED — per-call finality makes every window final and
recorded TTS inputs carry no is_final. Error propagation and cancel drain
work; only the finality guarantees are missing.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Iterator

import pytest

from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from avatar.engines.mock import MockRenderBackend
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow, TextChunk, VideoWindow
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.orchestrator import StreamOrchestrator
from backend.application.text_chunker import ChunkDecisionReason, TextChunker
from backend.application.render import llm_stream_controller as lsc
from backend.application.render import orchestrator as orch_module
from backend.application.render.llm_stream_controller import (
    DeltaEvent,
    EofEvent,
    ErrorEvent,
    LLMStreamController,
)
from tts.engines.base import AudioChunk, TTSEngine, TTSRequest


# ---------- helpers / stubs ----------


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


class _StallingLLM(LLMEngine):
    """LLM stub: yield one delta, then block until ``release`` is set.

    ``stalled_event`` is set BEFORE ``release.wait()`` so the stall is
    observable event-based (no sleep-polling). The bounded wait's
    TimeoutError is a harness failure, never the intended red.
    """

    name = "stall-llm"
    STALL_SAFETY_TIMEOUT = 10.0

    def __init__(self, first_delta: str, release: threading.Event) -> None:
        self._first_delta = first_delta
        self._release = release
        self.stalled_event = threading.Event()

    @classmethod
    def from_config(cls, cfg: dict) -> "_StallingLLM":  # pragma: no cover
        return cls("", threading.Event())

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        yield TextChunk(
            session_id=session_id,
            utterance_id=utterance_id,
            seq=0,
            text=self._first_delta,
            is_final=False,
        )
        self.stalled_event.set()
        if not self._release.wait(self.STALL_SAFETY_TIMEOUT):
            raise TimeoutError("release event never set — test harness failure")


class _RaisingLLM(LLMEngine):
    """LLM stub: yield a renderable first phrase, then (after a gate) raise."""

    name = "raising-llm"

    def __init__(self, first_delta: str, raise_gate: threading.Event) -> None:
        self._first_delta = first_delta
        self._raise_gate = raise_gate

    @classmethod
    def from_config(cls, cfg: dict) -> "_RaisingLLM":  # pragma: no cover
        return cls("", threading.Event())

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        yield TextChunk(
            session_id=session_id,
            utterance_id=utterance_id,
            seq=0,
            text=self._first_delta,
            is_final=False,
        )
        if not self._raise_gate.wait(10.0):
            raise TimeoutError("raise gate never set — test harness failure")
        raise RuntimeError("llm stream failed mid-utterance")


class _StubTTS(TTSEngine):
    """TTS stub modeling production per-call finality (tts/engines/base.py).

    Last window of each call is is_final=True, text_span carries the input,
    input finality ignored. Records: ``received_inputs`` (actual objects, so
    tests can inspect is_final), ``spoken_texts``, ``phrase_rendered`` (event
    set on every call — deterministic phrase-reached-TTS signal).
    """

    name = "stub-tts"
    sample_rate = 24000

    def __init__(self) -> None:
        self._seq = 0
        self.spoken_texts: list[str] = []
        self.received_inputs: list[object] = []
        self.phrase_rendered = threading.Event()

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
        self.phrase_rendered.set()
        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
        sid = text_or_chunk.session_id if isinstance(text_or_chunk, TextChunk) else session_id
        uid = text_or_chunk.utterance_id if isinstance(text_or_chunk, TextChunk) else utterance_id
        self.received_inputs.append(text_or_chunk)
        self.spoken_texts.append(text)
        # Per-call finality (production contract): this call's single window
        # is its last window -> always final, regardless of chunk finality.
        aw = AudioWindow(
            session_id=sid,
            utterance_id=uid,
            seq=self._seq,
            sample_rate=24000,
            duration_ms=200,
            pcm=b"\x01\x00" * 4800,  # 200 ms at 24 kHz int16
            is_final=True,
            text_span=text,
        )
        self._seq += 1
        yield aw


class _FailingTTS(_StubTTS):
    """TTS stub: yield one window (per-call finality applies), then raise."""

    name = "failing-tts"

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
        self.phrase_rendered.set()
        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
        sid = text_or_chunk.session_id if isinstance(text_or_chunk, TextChunk) else session_id
        uid = text_or_chunk.utterance_id if isinstance(text_or_chunk, TextChunk) else utterance_id
        self.received_inputs.append(text_or_chunk)
        self.spoken_texts.append(text)
        aw = AudioWindow(
            session_id=sid,
            utterance_id=uid,
            seq=self._seq,
            sample_rate=24000,
            duration_ms=200,
            pcm=b"\x01\x00" * 4800,
            is_final=True,  # per-call finality: last window of this call
            text_span=text,
        )
        self._seq += 1
        yield aw
        raise RuntimeError("tts stream failed mid-utterance")


def _build_orchestrator(
    llm: LLMEngine,
    tts: TTSEngine,
    max_queue: int = 20,
    flush_timeout_ms: int = 50,
    audio_window_callback=None,
) -> tuple[StreamOrchestrator, MockRenderBackend, BoundedVideoQueue, CoordinatorMetrics]:
    backend = MockRenderBackend()
    backend.start(StartOptions())
    queue = BoundedVideoQueue(max_size=max_queue)
    metrics = CoordinatorMetrics()
    cfg = {
        "text_chunk_min_chars": 4,
        "text_chunk_target_chars": 20,
        "text_chunk_max_chars": 40,
        "text_chunk_flush_timeout_ms": flush_timeout_ms,
    }
    orch = StreamOrchestrator(
        llm=llm,
        tts=tts,
        backend=backend,
        queue=queue,
        metrics=metrics,
        config=cfg,
        audio_window_callback=audio_window_callback,
    )
    return orch, backend, queue, metrics


def _start_session(backend: MockRenderBackend) -> str:
    return next(iter(backend._sessions.keys()))


def _capture_into(received: list[AudioWindow]):
    """Build an async audio_window_callback that appends into ``received``."""

    async def capture(window: AudioWindow) -> None:
        received.append(window)

    return capture


async def _drain(queue: BoundedVideoQueue) -> list[VideoWindow]:
    windows: list[VideoWindow] = []
    while queue.qsize() > 0:
        windows.append(await queue.get())
    return windows


async def _drain_video(
    queue: BoundedVideoQueue,
    stop: asyncio.Event,
    received: list[VideoWindow],
    first_captured: asyncio.Event,
) -> None:
    """Drain VideoWindows into ``received`` until ``stop`` is set (bounded gets)."""
    while not stop.is_set():
        try:
            w = await asyncio.wait_for(queue.get(), timeout=0.05)
        except asyncio.TimeoutError:
            continue
        received.append(w)
        first_captured.set()


def _assert_single_final_marker(windows: list[VideoWindow]) -> None:
    """Assert exactly one is_final window, and it is the last one."""
    finals = [i for i, w in enumerate(windows) if w.is_final]
    assert len(finals) == 1, f"expected exactly one final VideoWindow, got {finals}"
    assert finals[0] == len(windows) - 1, "the final marker must be on the last window"
    assert all(not w.is_final for w in windows[:-1]), "earlier windows must not be final"


# ---------- task 1.7: stalled-LLM deadline ----------


@pytest.mark.asyncio
async def test_stalled_llm_iterator_flushes_before_next_yield():
    """Task 1.7 (fixed): a stalled LLM must not block the 50 ms flush deadline."""
    release = threading.Event()
    llm = _StallingLLM(first_delta="Xin chào bạn", release=release)
    tts = _StubTTS()
    orch, backend, queue, metrics = _build_orchestrator(llm, tts)
    sid = _start_session(backend)

    task = asyncio.create_task(orch.run(sid, "hello"))
    try:
        await asyncio.to_thread(llm.stalled_event.wait, 2.0)
        assert llm.stalled_event.is_set(), "LLM must reach the stall point"
        await asyncio.to_thread(tts.phrase_rendered.wait, 2.0)
        assert tts.phrase_rendered.is_set(), (
            "no phrase reached TTS while the LLM was stalled — the flush deadline "
            "must be honored during the stall, not only at the next token"
        )
    finally:
        release.set()
        if not task.done():
            await asyncio.wait_for(task, timeout=5.0)

    spoken = await asyncio.wait_for(task, timeout=5.0)
    assert spoken, "run() must return non-empty spoken text"
    windows = await _drain(queue)
    assert windows[-1].is_final is True


@pytest.mark.asyncio
async def test_stalled_llm_iterator_cleanup_on_release():
    """BASELINE PASS: the stall harness is sound — releasing promptly completes."""
    release = threading.Event()
    llm = _StallingLLM(first_delta="Xin chào bạn", release=release)
    tts = _StubTTS()
    orch, backend, queue, metrics = _build_orchestrator(llm, tts)
    sid = _start_session(backend)

    task = asyncio.create_task(orch.run(sid, "hello"))
    try:
        release.set()
        spoken = await asyncio.wait_for(task, timeout=5.0)
    finally:
        release.set()
        if not task.done():
            await asyncio.wait_for(task, timeout=5.0)

    assert spoken, "run() must return non-empty spoken text"
    windows = await _drain(queue)
    assert windows, "at least one VideoWindow must be emitted"
    assert windows[-1].is_final is True


# ---------- task 2.6: decision_reason preserved through finality ----------


@pytest.mark.asyncio
async def test_final_reconstruction_preserves_decision_reason():
    """The orchestrator's final-marker reconstruction keeps decision_reason.

    finalize() splits the pending buffer into a non-final fixed_fallback
    head plus the exact final remainder (stamped FINALIZE); the orchestrator
    re-stamps the LAST emitted phrase is_final=True, and the reconstructed
    TextChunk must carry the original decision_reason — the reason is part
    of the canonical chunk contract and must not be dropped.
    """
    # The single delta is 19 chars ending at a whitespace (the only
    # qualifying split), under max_chars=40 so feed() holds the buffer and
    # finalize() splits it at the whitespace nearest target_chars=20: a
    # non-final fixed_fallback head plus the exact final remainder. The
    # orchestrator re-stamps the LAST emitted phrase as final, and the
    # reconstruction must preserve the original decision_reason.
    llm = _StubLLM(["a" * 18 + " "])
    tts = _StubTTS()
    orch, backend, queue, metrics = _build_orchestrator(llm, tts)
    sid = _start_session(backend)

    await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)

    final_input = tts.received_inputs[-1]
    assert getattr(final_input, "is_final", None) is True
    assert getattr(final_input, "decision_reason", None) == "finalize"


@pytest.mark.asyncio
async def test_speak_verbatim_passes_canonical_textchunk_to_tts():
    """``speak_verbatim`` hands TTS the canonical TextChunk, not a bare str.

    The canonical seam: ``_speak_verbatim_sync`` builds a ``TextChunk``
    carrying the session/utterance ids and must pass that object into
    ``stream_audio`` (the structural ``TextChunkLike`` protocol accepts any
    chunk-like object without importing the backend package). Asserting the
    exact canonical class pins the object identity, not just duck typing.
    """
    from backend.application.speech_chunking.types import TextChunk as CanonicalTextChunk

    llm = _StubLLM([])
    tts = _StubTTS()
    orch, backend, queue, metrics = _build_orchestrator(llm, tts)
    sid = _start_session(backend)

    spoken = await asyncio.wait_for(orch.speak_verbatim(sid, "Xin chào bạn."), timeout=5.0)

    assert spoken == "Xin chào bạn."
    assert len(tts.received_inputs) == 1
    chunk = tts.received_inputs[0]
    assert isinstance(chunk, CanonicalTextChunk)
    assert chunk.text == "Xin chào bạn."
    assert chunk.session_id == sid
    assert chunk.utterance_id == tts.received_inputs[0].utterance_id
    assert chunk.is_final is True


# ---------- task 1.8: E2E finality ----------


@pytest.mark.asyncio
async def test_normal_completion_exactly_one_final_video_window():
    """INTENDED RED (task 1.8): normal completion emits exactly one final marker.

    Three phrase deltas -> exactly one final VideoWindow (the last), one final
    AudioWindow (the last), exact TTS phrase sequence, exactly one recorded
    input carrying is_final (the last). Baseline per-call finality yields
    finals [0,1,2]; red lands on ``_assert_single_final_marker``.
    """
    llm = _StubLLM(["Xin chào.", "Bạn khỏe không?", "Cảm ơn!"])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(
        llm, tts, audio_window_callback=_capture_into(received)
    )
    sid = _start_session(backend)

    await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)

    windows = await _drain(queue)
    _assert_single_final_marker(windows)
    assert tts.spoken_texts == ["Xin chào.", "Bạn khỏe không?", "Cảm ơn!"], (
        "TTS must be called with the exact phrase sequence"
    )
    final_inputs = [
        i for i, x in enumerate(tts.received_inputs) if getattr(x, "is_final", None) is True
    ]
    assert final_inputs == [len(tts.received_inputs) - 1], (
        f"exactly the LAST TTS input must carry is_final, got {final_inputs}"
    )
    audio_finals = [i for i, w in enumerate(received) if w.is_final]
    assert len(audio_finals) == 1, f"expected exactly one final AudioWindow, got {audio_finals}"
    assert audio_finals[0] == len(received) - 1, "final AudioWindow must be the last one"


@pytest.mark.asyncio
async def test_empty_final_remainder_does_not_create_empty_terminal_artifact():
    """INTENDED RED (task 1.8): finalize must not fabricate an empty artifact.

    Last delta ends with punctuation, so the buffer is flushed before the
    final token: exactly two TTS calls, one final input (the last), one final
    AudioWindow and VideoWindow (the last), non-empty text_spans, two windows
    total. Baseline makes both windows final; red on the marker assert.
    """
    llm = _StubLLM(["Xin chào.", "Tạm biệt!"])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(
        llm, tts, audio_window_callback=_capture_into(received)
    )
    sid = _start_session(backend)

    await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)

    windows = await _drain(queue)
    _assert_single_final_marker(windows)
    assert tts.spoken_texts == ["Xin chào.", "Tạm biệt!"], (
        "exactly one TTS call per phrase — no extra synthesis call"
    )
    final_inputs = [
        i for i, x in enumerate(tts.received_inputs) if getattr(x, "is_final", None) is True
    ]
    assert final_inputs == [len(tts.received_inputs) - 1], (
        f"exactly the LAST TTS input must carry is_final, got {final_inputs}"
    )
    audio_finals = [i for i, w in enumerate(received) if w.is_final]
    assert len(audio_finals) == 1, f"expected exactly one final AudioWindow, got {audio_finals}"
    assert audio_finals[0] == len(received) - 1, "final AudioWindow must be the last one"
    assert len(received) == 2, "exactly one audio window per phrase — no fabricated extra artifact"
    assert all(w.text_span for w in received), "no audio window may carry an empty text_span"


@pytest.mark.asyncio
async def test_llm_error_does_not_emit_normal_final_marker():
    """INTENDED RED (task 1.8): an LLM stream error emits no final marker.

    Error must propagate from run(); no is_final may reach audio/video.
    Baseline: error propagates, but the pre-error window is final (per-call
    finality). Red lands on the zero-finals asserts.
    """
    raise_gate = threading.Event()
    llm = _RaisingLLM(first_delta="Xin chào bạn.", raise_gate=raise_gate)
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(
        llm, tts, audio_window_callback=_capture_into(received)
    )
    sid = _start_session(backend)

    task = asyncio.create_task(orch.run(sid, "hello"))
    try:
        await asyncio.to_thread(tts.phrase_rendered.wait, 2.0)
        assert tts.phrase_rendered.is_set(), "at least one phrase must render before the error"
        # Queue is NOT cleared on error, so a bounded get is safe: it captures
        # the pre-error VideoWindow deterministically.
        first_video = await asyncio.wait_for(queue.get(), timeout=1.0)
        raise_gate.set()
        with pytest.raises(RuntimeError, match="llm stream failed"):
            await asyncio.wait_for(task, timeout=5.0)
    finally:
        raise_gate.set()
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except Exception:
                pass

    assert received, "audio must have rendered before the LLM error"
    windows = [first_video] + await _drain(queue)
    assert windows, "at least one video window must have rendered before the error"
    assert all(not w.is_final for w in windows), "no final marker may reach the video queue"
    assert all(not w.is_final for w in received), "no final marker may reach the audio callback"


@pytest.mark.asyncio
async def test_tts_error_does_not_emit_normal_final_marker():
    """INTENDED RED (task 1.8): a TTS stream error emits no final marker.

    Error propagates; no is_final may reach audio/video. Baseline: error
    propagates but the pre-error window is final. Red on zero-finals asserts.
    """
    llm = _StubLLM(["Xin chào bạn", "Tạm biệt nhé!"])
    tts = _FailingTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(
        llm, tts, audio_window_callback=_capture_into(received)
    )
    sid = _start_session(backend)

    with pytest.raises(RuntimeError, match="tts stream failed"):
        await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)

    windows = await _drain(queue)
    assert all(not w.is_final for w in windows), "no final marker may reach the video queue"
    assert all(not w.is_final for w in received), "no final marker may reach the audio callback"


@pytest.mark.asyncio
async def test_cancellation_does_not_fabricate_normal_final_marker():
    """INTENDED RED (task 1.8): cancel mid-run must not fabricate a final marker.

    Long LLM cancelled while streaming; run completes without raising; no
    VideoWindow/AudioWindow with is_final. Baseline: pre-cancel windows are
    final. Red on zero-finals asserts.
    """
    llm = _StubLLM([f"chunk {i}." for i in range(50)])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(
        llm, tts, audio_window_callback=_capture_into(received)
    )
    sid = _start_session(backend)

    stop = asyncio.Event()
    first_video_captured = asyncio.Event()
    video_windows: list[VideoWindow] = []
    drainer = asyncio.create_task(_drain_video(queue, stop, video_windows, first_video_captured))

    task = asyncio.create_task(orch.run(sid, "long message"))
    try:
        await asyncio.to_thread(tts.phrase_rendered.wait, 2.0)
        assert received, "audio must render before the cancel"
        await asyncio.wait_for(first_video_captured.wait(), timeout=2.0)
        await orch.cancel(sid)
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        stop.set()
        await asyncio.wait_for(drainer, timeout=5.0)
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    assert video_windows, "the drainer must capture at least one video window before cancel"
    assert all(not w.is_final for w in video_windows), (
        "cancel must not fabricate a final marker in the video queue"
    )
    assert all(not w.is_final for w in received), (
        "cancel must not fabricate a final marker in audio"
    )


# ---------- task 4.x: bounded LLM stream controller ----------
#
# Not tested (deliberate): the stop-before-provider-invocation race in
# ``_produce`` — if ``stop()`` fires between the ``_stop`` check at thread
# start and the first ``stream_chunks`` call, the producer exits without
# invoking upstream (and closes a never-iterated generator). Scheduling
# cannot be forced deterministically without hooking thread internals; the
# race is handled by the code itself (checked both before invocation and
# after generator assignment), so a synthetic test would be flaky, not
# meaningful. Reviewed at 75bc5aa.


def _TC(sid: str, uid: str, seq: int, text: str, is_final: bool) -> TextChunk:
    return TextChunk(session_id=sid, utterance_id=uid, seq=seq, text=text, is_final=is_final)


def _build_controller(llm: LLMEngine, maxsize: int = 64) -> LLMStreamController:
    return LLMStreamController(
        llm,
        LLMRequest.from_prompt("hello"),
        session_id="sess-controller",
        utterance_id="utt-controller",
        maxsize=maxsize,
    )


def _drain_to_terminal(controller: LLMStreamController) -> list:
    events = []
    while True:
        event = controller.get(timeout=5.0)
        assert event is not None, "controller must emit a terminal event within 5s"
        events.append(event)
        if isinstance(event, (EofEvent, ErrorEvent)):
            return events


class _HookLLM(LLMEngine):
    """Injected stream_chunks behavior via ``on_call`` (yields/raises/stalls/close)."""

    name = "hook-llm"

    def __init__(self, on_call) -> None:
        self.on_call = on_call

    @classmethod
    def from_config(cls, cfg: dict) -> "_HookLLM":  # pragma: no cover
        return cls(lambda req, sid, uid: iter(()))

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("stub: use stream_chunks()")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        return self.on_call(req, session_id, utterance_id)


@pytest.mark.asyncio
async def test_stream_chunks_invoked_and_iterated_in_producer_thread():
    """4.1: invocation AND iteration happen on the producer thread."""
    invoke_thread: list[str] = []
    iterate_thread: list[str] = []
    consumer_name = threading.current_thread().name

    def on_call(req, sid, uid):
        invoke_thread.append(threading.current_thread().name)

        def gen():
            iterate_thread.append(threading.current_thread().name)
            yield _TC(sid, uid, 0, "Xin chào.", True)

        return gen()

    controller = _build_controller(_HookLLM(on_call))
    await asyncio.to_thread(controller.start)
    events = await asyncio.to_thread(_drain_to_terminal, controller)
    await asyncio.to_thread(controller.stop)

    assert invoke_thread == ["llm-stream-producer"], invoke_thread
    assert iterate_thread == ["llm-stream-producer"], iterate_thread
    assert "llm-stream-producer" != consumer_name
    assert isinstance(events[-1], EofEvent)
    assert [e for e in events if isinstance(e, DeltaEvent)] == [
        DeltaEvent(text="Xin chào.", is_final=True)
    ]


@pytest.mark.asyncio
async def test_error_event_without_trailing_eof():
    """4.1: an upstream exception surfaces as one ErrorEvent with no EOF."""
    raise_gate = threading.Event()

    def on_call(req, sid, uid):
        yield _TC(sid, uid, 0, "Xin chào.", False)
        if not raise_gate.wait(5.0):
            raise TimeoutError("raise gate never set — test harness failure")
        raise RuntimeError("boom mid-stream")

    controller = _build_controller(_HookLLM(on_call))
    await asyncio.to_thread(controller.start)
    try:
        first = await asyncio.to_thread(controller.get, 5.0)
        assert first == DeltaEvent(text="Xin chào.", is_final=False), first
        raise_gate.set()
        error = await asyncio.to_thread(controller.get, 5.0)
        assert isinstance(error, ErrorEvent), error
        assert isinstance(error.exc, RuntimeError) and str(error.exc) == "boom mid-stream"
        assert await asyncio.to_thread(controller.get, 0.2) is None, (
            "no event may follow the ErrorEvent"
        )
    finally:
        raise_gate.set()
        await asyncio.to_thread(controller.stop)


@pytest.mark.asyncio
async def test_base_exception_becomes_error_event_without_eof():
    """4.1: a custom BaseException surfaces as one ErrorEvent with no EOF.

    The producer must catch exceptions outside ``Exception`` (e.g.
    ``KeyboardInterrupt``) and carry them across the thread boundary verbatim;
    a bare ``except Exception`` would kill the producer thread silently and
    leave the consumer waiting forever on a terminal event.
    """
    exc = KeyboardInterrupt("custom base exception")

    def on_call(req, sid, uid):
        yield _TC(sid, uid, 0, "Xin chào.", False)
        raise exc

    controller = _build_controller(_HookLLM(on_call))
    await asyncio.to_thread(controller.start)
    try:
        first = await asyncio.to_thread(controller.get, 5.0)
        assert first == DeltaEvent(text="Xin chào.", is_final=False), first
        error = await asyncio.to_thread(controller.get, 5.0)
        assert isinstance(error, ErrorEvent), error
        assert error.exc is exc, "the original exception object must be preserved"
        assert await asyncio.to_thread(controller.get, 0.2) is None, (
            "no event may follow the ErrorEvent"
        )
    finally:
        await asyncio.to_thread(controller.stop)


@pytest.mark.asyncio
async def test_zero_deadline_returns_queued_event():
    """4.2: get(timeout=0) returns an event queued before the call (c6398a9 fix)."""
    queued = threading.Event()

    def on_call(req, sid, uid):
        yield _TC(sid, uid, 0, "Xin chào.", True)
        queued.set()

    controller = _build_controller(_HookLLM(on_call))
    await asyncio.to_thread(controller.start)
    try:
        await asyncio.to_thread(queued.wait, 5.0)
        assert queued.is_set(), "delta must be queued before the zero-deadline get"
        assert await asyncio.to_thread(controller.get, 0) == DeltaEvent(
            text="Xin chào.", is_final=True
        )
    finally:
        await asyncio.to_thread(controller.stop)


@pytest.mark.asyncio
async def test_tiny_queue_backpressures_without_drop_reorder_duplicate():
    """4.4: maxsize=1 backpressure blocks the producer; drain is exact, one EOF."""
    first_delta_gate = threading.Event()
    blocked_after_marker = threading.Event()

    def on_call(req, sid, uid):
        for i in range(30):
            yield _TC(sid, uid, i, f"delta {i} ", i == 29)
            if i == 0:
                first_delta_gate.set()
            elif i == 1:
                # Only reachable after put(delta 1) succeeded (slot drained).
                blocked_after_marker.set()

    controller = _build_controller(_HookLLM(on_call), maxsize=1)
    await asyncio.to_thread(controller.start)
    try:
        await asyncio.to_thread(first_delta_gate.wait, 5.0)
        assert first_delta_gate.is_set()
        await asyncio.to_thread(blocked_after_marker.wait, 0.2)
        assert not blocked_after_marker.is_set(), (
            "producer must be blocked on the full queue, not advancing past yield 1"
        )

        first = await asyncio.to_thread(controller.get, 5.0)
        assert first == DeltaEvent(text="delta 0 ", is_final=False), first
        await asyncio.to_thread(blocked_after_marker.wait, 5.0)
        assert blocked_after_marker.is_set(), "draining one slot must unblock the producer put"

        rest = await asyncio.to_thread(_drain_to_terminal, controller)
        deltas = [e for e in rest if isinstance(e, DeltaEvent)]
        assert [d.text for d in deltas] == [f"delta {i} " for i in range(1, 30)], (
            "exact order, no drops, no duplicates"
        )
        assert isinstance(rest[-1], EofEvent)
    finally:
        await asyncio.to_thread(controller.stop)


@pytest.mark.asyncio
async def test_stop_unblocks_producer_blocked_in_put():
    """4.4: stop() unblocks a producer stuck in _put on a full queue.

    The marker proves the producer reached put(delta 1) with maxsize=1 and
    an idle consumer, so it is blocked in the full-queue poll; thread
    termination after stop() proves the stop-responsive put.
    """
    reached_second_put = threading.Event()

    def on_call(req, sid, uid):
        for i in range(10):
            if i == 1:
                # put(delta 0) filled the only slot; put(delta 1) will block.
                reached_second_put.set()
            yield _TC(sid, uid, i, f"delta {i} ", i == 9)

    controller = _build_controller(_HookLLM(on_call), maxsize=1)
    await asyncio.to_thread(controller.start)
    try:
        await asyncio.to_thread(reached_second_put.wait, 5.0)
        assert reached_second_put.is_set(), "producer must reach the second put"
        await asyncio.to_thread(controller.stop)
        assert not controller._thread.is_alive(), (
            "producer thread must terminate after stop() unblocks the put"
        )
    finally:
        await asyncio.to_thread(controller.stop)


@pytest.mark.asyncio
async def test_stop_responsive_get_returns_none(monkeypatch):
    """4.4: stop() unblocks a pending get(None) with None, promptly.

    Queue empty + producer stalled: the waiter is blocked in the poll loop.
    JOIN_TIMEOUT_S monkeypatched small so the join returns promptly.
    """
    monkeypatch.setattr(lsc, "JOIN_TIMEOUT_S", 0.05)
    release = threading.Event()
    first_delta_gate = threading.Event()

    def on_call(req, sid, uid):
        yield _TC(sid, uid, 0, "Xin chào.", False)
        first_delta_gate.set()
        if not release.wait(5.0):
            raise TimeoutError("release never set")

    controller = _build_controller(_HookLLM(on_call))
    await asyncio.to_thread(controller.start)
    try:
        await asyncio.to_thread(first_delta_gate.wait, 5.0)
        assert first_delta_gate.is_set()
        first = await asyncio.to_thread(controller.get, 5.0)
        assert first == DeltaEvent(text="Xin chào.", is_final=False), first

        waiter = asyncio.create_task(asyncio.to_thread(controller.get, None, None))
        await asyncio.to_thread(controller.stop)
        assert await asyncio.wait_for(waiter, timeout=1.0) is None, (
            "stop must unblock the pending get with None"
        )
    finally:
        release.set()
        await asyncio.to_thread(controller.stop)


@pytest.mark.asyncio
async def test_external_cancel_responsive_get_returns_none():
    """4.4: an external cancel event unblocks a pending get(None) with None.

    Waiter polls the empty queue, re-checking the external cancel event each
    poll; setting it — NOT stop() — must return None. stop() for cleanup.
    """
    release = threading.Event()
    first_delta_gate = threading.Event()

    def on_call(req, sid, uid):
        yield _TC(sid, uid, 0, "Xin chào.", False)
        first_delta_gate.set()
        if not release.wait(5.0):
            raise TimeoutError("release never set")

    controller = _build_controller(_HookLLM(on_call))
    await asyncio.to_thread(controller.start)
    try:
        await asyncio.to_thread(first_delta_gate.wait, 5.0)
        assert first_delta_gate.is_set()
        first = await asyncio.to_thread(controller.get, 5.0)
        assert first == DeltaEvent(text="Xin chào.", is_final=False), first

        cancel = threading.Event()
        waiter = asyncio.create_task(asyncio.to_thread(controller.get, None, cancel))
        cancel.set()
        assert await asyncio.wait_for(waiter, timeout=1.0) is None, (
            "an external cancel must unblock the pending get with None"
        )
    finally:
        release.set()
        await asyncio.to_thread(controller.stop)


@pytest.mark.asyncio
async def test_cancel_closes_suspended_generator_and_thread_ends(monkeypatch):
    """4.4: repeated stop() closes the suspended generator exactly once.

    A custom close-aware iterator's ``close()`` is not required to be
    repeatable, so the controller must guard close-at-most-once: invoking
    ``stop()`` three times must call ``close()`` once and still terminate the
    producer thread. (A second ``stop()`` is also the typical cleanup path —
    the ``finally`` of the first.)
    """
    monkeypatch.setattr(lsc, "JOIN_TIMEOUT_S", 0.05)
    close_count = 0
    stop_wait = threading.Event()

    def on_call(req, sid, uid):
        class _CloseAware:
            def __iter__(self):
                yield _TC(sid, uid, 0, "Xin chào.", False)
                stop_wait.wait()

            def close(self):
                nonlocal close_count
                close_count += 1
                stop_wait.set()

        return _CloseAware()

    controller = _build_controller(_HookLLM(on_call))
    await asyncio.to_thread(controller.start)
    try:
        first = await asyncio.to_thread(controller.get, 5.0)
        assert first == DeltaEvent(text="Xin chào.", is_final=False), first
        await asyncio.to_thread(controller.stop)
        await asyncio.to_thread(controller.stop)
        await asyncio.to_thread(controller.stop)
        assert close_count == 1, f"close() must run exactly once, got {close_count}"
        assert not controller._thread.is_alive(), "producer thread must terminate"
    finally:
        await asyncio.to_thread(controller.stop)


@pytest.mark.asyncio
async def test_stop_retries_close_failed_while_provider_active(monkeypatch):
    """dd50ce5: a close that fails while the provider is active must be retried.

    The custom iterator blocks inside its first ``__next__`` (provider I/O in
    flight), so stop()'s close from the consumer thread raises ValueError
    (mid-execution, like a real generator) and the join is bounded. Once the
    release event lets ``__next__`` return one delta, the producer's top-of-
    body stop check retries the close successfully: exactly one successful
    close, at least two close attempts, and no further ``__next__`` after
    stop. A cleanup stop() must not close it again.
    """
    monkeypatch.setattr(lsc, "JOIN_TIMEOUT_S", 0.05)
    active = threading.Event()
    release = threading.Event()

    class _BlockingIterator:
        def __init__(self, tc: TextChunk) -> None:
            self._tc = tc
            self.next_calls = 0
            self.close_attempts = 0
            self.close_successes = 0
            self._in_next = False
            self._closed = False

        def __iter__(self):
            return self

        def __next__(self) -> TextChunk:
            self.next_calls += 1
            self._in_next = True
            try:
                active.set()
                if not release.wait(10.0):
                    raise TimeoutError("release never set — test harness failure")
                return self._tc
            finally:
                self._in_next = False

        def close(self) -> None:
            self.close_attempts += 1
            if self._in_next:
                # Mid-__next__ in the producer thread: closing from the
                # consumer thread is unsupported (real generators raise).
                raise ValueError("generator is executing in another thread")
            if not self._closed:
                self._closed = True
                self.close_successes += 1

    iterator = _BlockingIterator(_TC("sess-controller", "utt-controller", 0, "Xin chào.", False))
    controller = _build_controller(_HookLLM(lambda req, sid, uid: iterator))
    await asyncio.to_thread(controller.start)
    try:
        await asyncio.to_thread(active.wait, 5.0)
        assert active.is_set(), "producer must be blocked inside the first __next__"
        await asyncio.to_thread(controller.stop)
        assert controller._thread.is_alive(), (
            "join must be bounded while the provider is still blocked"
        )
        release.set()
        await asyncio.to_thread(controller._thread.join, 5.0)
        assert not controller._thread.is_alive(), "producer thread must terminate"
        assert iterator.next_calls == 1, (
            f"no __next__ may run after stop, got {iterator.next_calls}"
        )
        assert iterator.close_successes == 1, (
            f"close() must succeed exactly once, got {iterator.close_successes}"
        )
        assert iterator.close_attempts >= 2, (
            "close must be retried after the failed consumer attempt, "
            f"got {iterator.close_attempts}"
        )
    finally:
        release.set()
        await asyncio.to_thread(controller.stop)
        assert iterator.close_successes == 1, "a cleanup stop() must not close the generator again"


@pytest.mark.asyncio
async def test_sub_min_buffer_deadline_is_none_until_min_reached():
    """4.3: sub-min buffer yields no deadline; min reached derives from start."""
    orch, _, _, _ = _build_orchestrator(_StubLLM([]), _StubTTS())
    chunker = TextChunker(
        session_id="sess-deadline",
        utterance_id="utt-deadline",
        min_chars=4,
        target_chars=20,
        max_chars=40,
        flush_timeout_ms=50,
    )

    assert orch._remaining_deadline(chunker) is None, "empty buffer must have no deadline"

    chunker.feed("X")
    assert len(chunker.buffered_text) < chunker.min_chars
    assert orch._remaining_deadline(chunker) is None, "sub-min buffer must have no deadline"

    started = chunker.buffer_started_at
    chunker.feed("Y")
    chunker.feed("Z")
    chunker.feed("W")
    assert len(chunker.buffered_text) == 4
    assert chunker.buffer_started_at == started, (
        "buffer start must be the FIRST fragment's timestamp"
    )
    deadline = orch._remaining_deadline(chunker)
    assert deadline is not None and deadline <= 0.05, (
        f"deadline must derive from buffer_started_at, got {deadline!r}"
    )

    chunker.flush(reason=ChunkDecisionReason.LATENCY_DEADLINE)
    assert chunker.buffer_started_at is None, "flush must reset the buffer clock"
    assert orch._remaining_deadline(chunker) is None, (
        "post-flush empty buffer must have no deadline"
    )


@pytest.mark.asyncio
async def test_constructor_rejects_non_positive_maxsize():
    """4.4: maxsize 0/negative are rejected (Queue(maxsize=0) is unbounded)."""
    with pytest.raises(ValueError, match="maxsize"):
        _build_controller(_HookLLM(lambda req, sid, uid: iter(())), maxsize=0)
    with pytest.raises(ValueError, match="maxsize"):
        _build_controller(_HookLLM(lambda req, sid, uid: iter(())), maxsize=-1)


def test_cleanup_raise_still_emits_bridge_sentinel(monkeypatch):
    """1.8/4.4: a raising controller.stop() propagates AND emits the sentinel.

    ``_run_sync`` puts ``_BRIDGE_SENTINEL`` in a nested ``finally`` around
    ``controller.stop()``: the async ``run()`` drain blocks forever in
    ``bridge.get`` if the sentinel is skipped, so a cleanup failure must not
    swallow the sentinel — and the exception must still propagate (never
    swallowed). The LLM EOFs immediately, so the producer thread is already
    finished when cleanup raises and nothing leaks.
    """

    def raising_stop(self) -> None:
        raise RuntimeError("controller cleanup failed")

    monkeypatch.setattr(LLMStreamController, "stop", raising_stop)
    orch, _, _, _ = _build_orchestrator(_StubLLM([]), _StubTTS())
    bridge: queue.Queue = queue.Queue()

    with pytest.raises(RuntimeError, match="controller cleanup failed"):
        orch._run_sync("sess-cleanup", "utt-cleanup", LLMRequest.from_prompt("hello"), bridge)

    sentinel = bridge.get_nowait()
    assert sentinel is orch_module._BRIDGE_SENTINEL, (
        "the bridge sentinel must reach the async drain even when cleanup raises"
    )
    assert bridge.empty(), "the sentinel must be the last item on the bridge"
