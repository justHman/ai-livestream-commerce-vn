"""Phase-1 regression tests for OpenSpec adaptive-speech-text-chunking.

Covers task 1.7 (orchestrator-level stalled-LLM deadline) and task 1.8
(E2E finality guarantees: exactly one final marker, no empty terminal
artifact, no final marker on error/cancel).

Stub-engine pattern mirrors test_playback_queue.py: stub LLM yielding
TextChunk deltas, stub TTS yielding one AudioWindow per phrase, real
MockRenderBackend for the video stage.

The TTS stub models PRODUCTION per-call finality (``TTSEngine.stream_audio``
in tts/engines/base.py): the LAST window of every synthesis call is marked
``is_final=True`` and ``text_span`` carries the input text on every window,
regardless of the input chunk's finality. The orchestrator currently passes
``phrase.text`` (a plain string) into ``tts.stream_audio``, so no finality
information survives the TTS boundary: every window arrives final unless the
orchestrator normalizes per-call finals into exactly one utterance-level
final (one-window lookahead / end-of-utterance stamping).

Intended-failure map on the current baseline (HEAD 486b4f5, observed 2026-08-09):
  - test_stalled_llm_iterator_flushes_before_next_yield: INTENDED RED (task
    1.7). ``_run_sync`` only calls ``chunker.feed()`` /
    ``chunker.check_timeout()`` inside the ``for token in
    stream_chunks(...)`` loop, so a stalled (non-yielding) synchronous
    generator suspends the whole pipeline and the flush deadline is never
    honored until the next token arrives. Observed: ``tts.phrase_rendered``
    is never set while the LLM is stalled (no phrase reaches TTS).
  - test_normal_completion_exactly_one_final_video_window /
    test_empty_final_remainder_does_not_create_empty_terminal_artifact /
    test_llm_error_does_not_emit_normal_final_marker /
    test_tts_error_does_not_emit_normal_final_marker /
    test_cancellation_does_not_fabricate_normal_final_marker: INTENDED RED
    (task 1.8, finality normalization). Per-call finality makes every window
    final (a normal 3-phrase run yields finals [0, 1, 2]) and the recorded
    TTS inputs are plain strings carrying no ``is_final`` — the orchestrator
    must normalize to exactly one utterance-level final and stamp nothing
    final on error/cancel. Error propagation and cancel drain DO work on
    baseline; only the finality guarantees are missing.
  - test_stalled_llm_iterator_cleanup_on_release: BASELINE PASS (harness
    sanity: releasing the stall promptly completes a full run).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Iterator

import pytest

from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from avatar.engines.mock import MockRenderBackend
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow, TextChunk, VideoWindow
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.orchestrator import StreamOrchestrator
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
    """LLM stub that yields one delta, then blocks until a release event.

    Models a synchronous generator that stalls (no next token) mid-utterance.
    ``stalled_event`` is set immediately BEFORE entering ``release.wait()``
    so the test can deterministically observe the stall (event-based, no
    sleep-polling). ``release.wait()`` is bounded by ``STALL_SAFETY_TIMEOUT``
    (far longer than any monitor wait): a TimeoutError there means the test
    itself forgot to release — a harness failure, never the intended red.
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
    """LLM stub that yields a renderable delta, blocks, then raises.

    The first delta is a complete phrase ("Xin chào bạn." — punctuation
    flush with min_chars=4), so a window renders BEFORE the error fires.
    ``raise_gate`` controls when the error is raised; the bounded wait means
    an unset gate is a harness failure, never the intended red.
    """

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
    """TTS stub modeling PRODUCTION per-call finality.

    Mirror of ``TTSEngine.stream_audio`` (tts/engines/base.py): accepts a
    TextChunk or a plain string, reads only text/session_id/utterance_id,
    marks the LAST window of every synthesis call ``is_final=True``
    (per-call finality), and sets ``text_span`` to the input text on every
    window. Input finality (``TextChunk.is_final``) is IGNORED, exactly like
    production.

    Records:
      - ``received_inputs``: the ACTUAL object passed (str or TextChunk), so
        tests can inspect ``getattr(input, "is_final", None)`` — on baseline
        the orchestrator passes plain strings and nothing carries is_final.
      - ``spoken_texts``: the text of each synthesis call.
      - ``phrase_rendered``: threading.Event set at the top of every
        ``stream_audio`` call (thread-safe), so tests can deterministically
        confirm a phrase reached TTS without sleep-polling.
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
    """TTS stub that yields one window, then raises RuntimeError on the next.

    Per-call finality applies like _StubTTS: the single window it yields is
    the last of its call, so it is is_final=True unless the orchestrator
    normalizes it. Records inputs/spoken_texts the same way.
    """

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
    """Concurrently drain VideoWindows into ``received`` until ``stop`` is set.

    Each get is bounded (0.05 s), so the drainer always terminates quickly
    after ``stop`` is set. Races ``clear()`` on cancel harmlessly (both
    dequeue).
    """
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
    """INTENDED RED (task 1.7): a stalled LLM generator must not block flushing.

    The 50 ms flush deadline must be honored while the LLM iterator is
    stalled (no next token): the buffered text must reach TTS before the
    release event is set. On baseline, ``_run_sync`` only polls the chunker
    inside the stream loop, so nothing flushes during the stall and
    ``tts.phrase_rendered`` is never set. The red lands on
    ``assert tts.phrase_rendered.is_set()``.
    """
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
        # INTENDED RED on baseline: no flush during the stall.
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
    """BASELINE PASS: the stall harness is sound — releasing promptly completes.

    Same stalling LLM, but the release event is set immediately after start:
    the run must complete, return non-empty spoken text, and end with a final
    VideoWindow. Proves the harness itself works (no import/setup failure)
    and isolates the task-1.7 defect to the stall case.
    """
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


# ---------- task 1.8: E2E finality ----------


@pytest.mark.asyncio
async def test_normal_completion_exactly_one_final_video_window():
    """INTENDED RED (task 1.8): normal completion emits exactly one final marker.

    Three phrase deltas -> exactly one VideoWindow with is_final=True and it
    is the last window; all earlier windows non-final. The audio path must
    mirror this: exactly one final AudioWindow (the last one received). TTS
    must be called with the exact phrase sequence, and exactly ONE recorded
    input carries is_final=True — the last one.

    Observed on baseline: production per-call finality makes every window
    final (finals [0, 1, 2]) and the orchestrator passes plain strings into
    ``tts.stream_audio``, so no recorded input carries is_final. The red
    lands on ``_assert_single_final_marker`` first.
    """
    llm = _StubLLM(["Xin chào.", "Bạn khỏe không?", "Cảm ơn!"])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
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
    """INTENDED RED (task 1.8): no empty terminal artifact from a finalize.

    The last delta ends with punctuation, so the buffer is already flushed
    when the final token arrives: finalize must not fabricate an empty final
    chunk. Exactly two TTS calls (one per phrase, no extra synthesis call),
    exactly one recorded input final (the last), exactly one final
    AudioWindow (the last) and one final VideoWindow (the last), every audio
    window's text_span non-empty, and exactly two audio windows total.

    Observed on baseline: per-call finality makes both windows final
    (finals [0, 1]). The red lands on ``_assert_single_final_marker``.
    """
    llm = _StubLLM(["Xin chào.", "Tạm biệt!"])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
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

    The stub LLM yields a RENDERABLE first phrase, then (after a gate) raises
    RuntimeError. The error must propagate out of ``run()``, and neither the
    audio callback nor any drained video window may carry is_final=True —
    the pre-error window must be normalized to non-final, not stamped final.

    Observed on baseline: the error propagates cleanly (good), but the window
    rendered before the failure arrives is_final=True because of production
    per-call finality. The red lands on the zero-finals assertions.
    """
    raise_gate = threading.Event()
    llm = _RaisingLLM(first_delta="Xin chào bạn.", raise_gate=raise_gate)
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
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

    The stub TTS yields one AudioWindow for the first phrase (per-call
    finality: it is the last window of its call), then raises RuntimeError.
    The error must propagate out of ``run()`` and no is_final=True marker
    may reach the video queue or the audio callback.

    Observed on baseline: the error DOES propagate (good), but the window
    rendered before the failure arrives is_final=True because of production
    per-call finality. The red lands on the zero-finals assertions.
    """
    llm = _StubLLM(["Xin chào bạn", "Tạm biệt nhé!"])
    tts = _FailingTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
    sid = _start_session(backend)

    with pytest.raises(RuntimeError, match="tts stream failed"):
        await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)

    windows = await _drain(queue)
    assert all(not w.is_final for w in windows), "no final marker may reach the video queue"
    assert all(not w.is_final for w in received), "no final marker may reach the audio callback"


@pytest.mark.asyncio
async def test_cancellation_does_not_fabricate_normal_final_marker():
    """INTENDED RED (task 1.8): cancel mid-run must not fabricate a final marker.

    A long-running LLM is cancelled while streaming; the run must complete
    without raising, drain the queue, and never emit any VideoWindow or
    AudioWindow with is_final=True — cancel is not a normal completion.
    Audio is captured via the callback (immune to the cancel queue clear);
    video is captured by a concurrent drainer that races ``clear()``
    harmlessly. The red lands on the zero-finals assertions: pre-cancel
    windows arrive is_final=True because of production per-call finality.
    """
    llm = _StubLLM([f"chunk {i}." for i in range(50)])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
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
    assert all(not w.is_final for w in received), "cancel must not fabricate a final marker in audio"
