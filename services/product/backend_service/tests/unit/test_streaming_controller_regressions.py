"""Phase-1 regression tests for OpenSpec adaptive-speech-text-chunking.

Covers task 1.7 (orchestrator-level stalled-LLM deadline) and task 1.8
(E2E finality guarantees: exactly one final marker, no empty terminal
artifact, no final marker on error/cancel).

Stub-engine pattern mirrors test_playback_queue.py: stub LLM yielding
TextChunk deltas, stub TTS yielding one AudioWindow per phrase, real
MockRenderBackend for the video stage.

Intended-failure map on the current baseline (HEAD 486b4f5, observed 2026-08-09):
  - test_stalled_llm_iterator_flushes_before_next_yield: INTENDED RED (task
    1.7). ``_run_sync`` only calls ``chunker.feed()`` /
    ``chunker.check_timeout()`` inside the ``for token in
    stream_chunks(...)`` loop, so a stalled (non-yielding) synchronous
    generator suspends the whole pipeline and the flush deadline is never
    honored until the next token arrives. Observed: no text reaches TTS
    before the release event.
  - test_normal_completion_exactly_one_final_video_window /
    test_empty_final_remainder_does_not_create_empty_terminal_artifact /
    test_tts_error_does_not_emit_normal_final_marker /
    test_cancellation_does_not_fabricate_normal_final_marker: INTENDED RED
    (task 1.8, finality normalization). Observed baseline behavior:
    ``_run_sync`` renders phrases by passing ``phrase.text`` (a plain
    string) into ``tts.stream_audio``, so the chunk's ``is_final`` is lost
    at the TTS boundary — the TTS stub marks every window final and every
    VideoWindow lands in the queue with ``is_final=True`` (a normal 3-phrase
    run yields finals [0, 1, 2]). Error propagation and cancel drain DO
    work on baseline; only the no-final-marker-on-error/cancel guarantees
    are missing.
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
    ``release.wait()`` is bounded by ``stall_seconds`` and raises
    TimeoutError if the event is never set, so a buggy controller cannot hang
    the test beyond the cap.
    """

    name = "stall-llm"

    def __init__(self, first_delta: str, release: threading.Event, stall_seconds: float = 1.0):
        self._first_delta = first_delta
        self._release = release
        self._stall_seconds = stall_seconds

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
        # Stall: block until released (bounded).
        if not self._release.wait(self._stall_seconds):
            raise TimeoutError("release event never set — controller must flush during stall")


class _RaisingLLM(LLMEngine):
    """LLM stub that yields one delta, then raises RuntimeError on the next token."""

    name = "raising-llm"

    def __init__(self, first_delta: str) -> None:
        self._first_delta = first_delta

    @classmethod
    def from_config(cls, cfg: dict) -> "_RaisingLLM":  # pragma: no cover
        return cls("")

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
        raise RuntimeError("llm stream failed mid-utterance")


class _StubTTS(TTSEngine):
    """TTS stub yielding one 200 ms AudioWindow per phrase, recording text spans.

    Mirror of the real TTS seam: ``stream_audio`` may be called with a
    TextChunk (is_final preserved) or with a plain string (nothing to
    propagate finality from — the orchestrator must not do this for phrase
    rendering). The string-input fallback defaults to is_final=True, which
    is exactly what a naive implementation produces.
    """

    name = "stub-tts"
    sample_rate = 24000

    def __init__(self) -> None:
        self._seq = 0
        self.spoken_texts: list[str] = []

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
        self.spoken_texts.append(text)
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


class _FailingTTS(_StubTTS):
    """TTS stub that yields one window, then raises RuntimeError on the next."""

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
        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
        sid = text_or_chunk.session_id if isinstance(text_or_chunk, TextChunk) else session_id
        uid = text_or_chunk.utterance_id if isinstance(text_or_chunk, TextChunk) else utterance_id
        is_final = text_or_chunk.is_final if isinstance(text_or_chunk, TextChunk) else True
        aw = AudioWindow(
            session_id=sid,
            utterance_id=uid,
            seq=self._seq,
            sample_rate=24000,
            duration_ms=200,
            pcm=b"\x01\x00" * 4800,
            is_final=is_final,
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
    stalled (no next token). The buffered text must reach TTS before the
    release event is set. On baseline, ``_run_sync`` only polls the chunker
    inside the stream loop, so nothing flushes during the stall and the TTS
    received-text list stays empty until release.
    """
    release = threading.Event()
    llm = _StallingLLM(first_delta="Xin chào bạn", release=release, stall_seconds=1.0)
    tts = _StubTTS()
    orch, backend, queue, metrics = _build_orchestrator(llm, tts)
    sid = _start_session(backend)
    received: list[AudioWindow] = []
    orch._audio_window_callback = _capture_into(received)

    task = asyncio.create_task(orch.run(sid, "hello"))

    # Poll for a phrase reaching TTS while the LLM is still stalled. On
    # baseline nothing flushes during the stall and this loop hits its
    # deadline with spoken_texts still empty — the intended red.
    deadline = asyncio.get_running_loop().time() + 1.0
    while not tts.spoken_texts:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.01)
    flushed_before_release = bool(tts.spoken_texts)

    release.set()
    spoken = await asyncio.wait_for(task, timeout=5.0)

    assert flushed_before_release, (
        "no phrase reached TTS while the LLM was stalled — the flush deadline "
        "must be honored during the stall, not only at the next token"
    )
    assert spoken
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
    llm = _StallingLLM(first_delta="Xin chào bạn", release=release, stall_seconds=1.0)
    tts = _StubTTS()
    orch, backend, queue, metrics = _build_orchestrator(llm, tts)
    sid = _start_session(backend)
    received: list[AudioWindow] = []
    orch._audio_window_callback = _capture_into(received)

    task = asyncio.create_task(orch.run(sid, "hello"))
    release.set()
    spoken = await asyncio.wait_for(task, timeout=5.0)

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
    mirror this: exactly one AudioWindow with is_final=True via
    ``audio_window_callback``, and it is the last one received. Observed on
    baseline: the orchestrator passes ``phrase.text`` (string) into
    ``tts.stream_audio``, the chunk's is_final is lost at the TTS boundary,
    and every window arrives final (finals [0, 1, 2]).
    """
    llm = _StubLLM(["Xin chào.", "Bạn khỏe không?", "Cảm ơn!"])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
    sid = _start_session(backend)

    await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)

    windows = await _drain(queue)
    _assert_single_final_marker(windows)
    audio_finals = [i for i, w in enumerate(received) if w.is_final]
    assert len(audio_finals) == 1, f"expected exactly one final AudioWindow, got {audio_finals}"
    assert audio_finals[0] == len(received) - 1, "final AudioWindow must be the last one"


@pytest.mark.asyncio
async def test_empty_final_remainder_does_not_create_empty_terminal_artifact():
    """INTENDED RED (task 1.8): no empty terminal artifact from a finalize.

    The last delta ends with punctuation, so the buffer is already flushed
    when the final token arrives: finalize must not fabricate an empty final
    chunk. The final marker must land on a non-empty window (video text span
    via audio text, and audio text itself) — and there must be exactly one
    final marker overall. Observed on baseline: every window arrives final
    because is_final is lost at the string-typed TTS boundary (finals [0, 1]).
    """
    llm = _StubLLM(["Xin chào.", "Tạm biệt!"])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
    sid = _start_session(backend)

    await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)

    windows = await _drain(queue)
    _assert_single_final_marker(windows)
    assert received, "audio callback must have received windows"
    assert received[-1].text_span, "final AudioWindow must carry non-empty text"
    assert received[-1].is_final is True


@pytest.mark.asyncio
async def test_llm_error_does_not_emit_normal_final_marker():
    """BASELINE PASS: an LLM stream error propagates and emits no final marker.

    The stub LLM yields one delta, then raises RuntimeError. The error must
    propagate out of ``run()`` (not be swallowed), and neither the video
    queue nor the audio callback may ever see is_final=True. Observed on
    baseline: the error propagates cleanly (no windows at all) — passes
    because the failure happens before any phrase is rendered.
    """
    llm = _RaisingLLM(first_delta="Xin chào bạn")
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
    sid = _start_session(backend)

    with pytest.raises(RuntimeError, match="llm stream failed"):
        await asyncio.wait_for(orch.run(sid, "hello"), timeout=5.0)

    windows = await _drain(queue)
    assert all(not w.is_final for w in windows), "no final marker may reach the video queue"
    assert all(not w.is_final for w in received), "no final marker may reach the audio callback"


@pytest.mark.asyncio
async def test_tts_error_does_not_emit_normal_final_marker():
    """INTENDED RED (task 1.8): a TTS stream error emits no final marker.

    The stub TTS yields one AudioWindow for the first phrase, then raises
    RuntimeError. The error must propagate out of ``run()`` and no
    is_final=True marker may reach the video queue or the audio callback.
    Observed on baseline: the error DOES propagate (good), but the one
    window rendered before the failure arrives is_final=True because the
    chunk's finality was lost at the string-typed TTS boundary.
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
    without raising, drain the queue, and never emit any VideoWindow (or
    AudioWindow) with is_final=True — cancel is not a normal completion.
    Observed on baseline: the run completes and the queue drains (good), but
    windows rendered before the cancel arrive is_final=True because the
    chunk's finality was lost at the string-typed TTS boundary.
    """
    llm = _StubLLM([f"chunk {i}." for i in range(50)])
    tts = _StubTTS()
    received: list[AudioWindow] = []
    orch, backend, queue, metrics = _build_orchestrator(llm, tts, audio_window_callback=_capture_into(received))
    sid = _start_session(backend)

    task = asyncio.create_task(orch.run(sid, "long message"))
    await asyncio.sleep(0.05)
    await orch.cancel(sid)
    await asyncio.wait_for(task, timeout=5.0)

    assert queue.qsize() == 0, "cancel must drain the queue"
    assert all(not w.is_final for w in received), "cancel must not fabricate a final marker"
