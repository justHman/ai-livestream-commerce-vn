"""Scripted (verbatim) turns: phrase boundaries and synthesis overlap.

Live test 2026-10-13: speech paused inside a word pair ("thoải | mái") and lagged between chunks.
Two causes in our pipeline: the 80-char live cap cut sentences at whitespace, and the next phrase
was synthesized only after the current one had been (almost) played out.

The concurrency tests use handshakes (events set by the fakes when something really starts), never
a fixed sleep to "let it start". Time bounds are only used to prove a call did NOT wait for a TTS
request that is held for 10 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Callable, Iterator

import pytest

from avatar.engines.mock import MockRenderBackend
from backend.application.render.engines_base import StartOptions
from backend.application.render.orchestrator import StreamOrchestrator
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.windows import AudioWindow
from backend.application.text_chunker import FixedChunkPolicyConfig
from llm.engines.base import LLMEngine

# One comma-free 105-char sentence (the old cap cut it at "thoải | mái"), then a short one.
LONG = (
    "Trường hợp mặc thử mà chưa thấy vừa ý thì vẫn được đổi size thoải mái "
    "trong vòng mười lăm ngày đó luôn nha. Cảm ơn mọi người nhiều."
)
FIRST, SECOND = "Trường", "Cảm ơn"  # the first six characters identify a phrase
HELD_S = 10  # how long a "hung" TTS request is held
PROMPT_S = 3  # a call that does not wait for the held request returns well inside this
TEARDOWN_S = 5  # upper bound for each teardown wait (task finished, worker exited)


class _NoLLM(LLMEngine):
    name = "no-llm"

    @classmethod
    def from_config(cls, cfg: dict) -> "_NoLLM":  # pragma: no cover
        return cls()

    def generate(self, req):  # pragma: no cover
        raise RuntimeError("verbatim never calls the LLM")


class _FakeTTS:
    """One 200 ms window per phrase. ``block_on`` is held on ``gate``; ``fail_on`` raises.

    ``begun``/``ended`` are per-phrase events, so tests wait for what really happened.
    """

    name = "fake-tts"

    def __init__(self, *, block_on: str = "", fail_on: str = "") -> None:
        self.block_on, self.fail_on = block_on, fail_on
        self.gate = threading.Event()
        self.calls: list[str] = []  # texts whose synthesis completed
        self._begun: dict[str, threading.Event] = {}
        self._ended: dict[str, threading.Event] = {}

    @staticmethod
    def _event(table: dict[str, threading.Event], key: str) -> threading.Event:
        return table.setdefault(key, threading.Event())

    def wait_begun(self, key: str, timeout: float = 5.0) -> bool:
        return self._event(self._begun, key).wait(timeout)

    def wait_ended(self, key: str, timeout: float = 5.0) -> bool:
        return self._event(self._ended, key).wait(timeout)

    def has_begun(self, key: str) -> bool:
        return self._event(self._begun, key).is_set()

    def has_ended(self, key: str) -> bool:
        return self._event(self._ended, key).is_set()

    def stream_audio(self, phrase, *, session_id="", utterance_id="", **_) -> Iterator[AudioWindow]:
        key = phrase.text.strip()[:6]
        self._event(self._begun, key).set()
        if self.fail_on and key == self.fail_on:
            raise TimeoutError("tts down")
        if self.block_on and key == self.block_on:
            self.gate.wait(HELD_S)
        self.calls.append(phrase.text)
        self._event(self._ended, key).set()
        yield AudioWindow(
            session_id=phrase.session_id,
            utterance_id=phrase.utterance_id,
            seq=len(self.calls),
            sample_rate=24000,
            duration_ms=200,
            pcm=b"\x01\x00" * 4800,
            is_final=True,
            text_span=phrase.text,
        )


class _Playback(MockRenderBackend):
    """stream_audio blocks like a real avatar: until the audio has (almost) played."""

    def __init__(self, play_s: float = 0.0, on_first: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.play_s = play_s
        self.on_first = on_first
        self.playing = threading.Event()  # set when the first window starts playing
        self.windows: list[AudioWindow] = []

    def stream_audio(self, session_id, audio_window):
        first = not self.playing.is_set()
        self.playing.set()
        if first and self.on_first is not None:
            self.on_first()
        time.sleep(self.play_s)  # the simulated playback itself; nothing asserts on its length
        self.windows.append(audio_window)
        return iter(())


def _orchestrator(tts, backend, *, max_chars: int = 80) -> StreamOrchestrator:
    backend.start(StartOptions())
    return StreamOrchestrator(
        llm=_NoLLM(),
        tts=tts,
        backend=backend,
        queue=BoundedVideoQueue(max_size=20),
        metrics=CoordinatorMetrics(),
        fixed_config=FixedChunkPolicyConfig(min_chars=12, target_chars=40, max_chars=max_chars),
    )


def _prefetch_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("tts-prefetch")]


async def _no_prefetch_threads(timeout: float = 5.0) -> bool:
    """True once the lookahead worker has exited (bounded wait on the condition)."""
    deadline = time.monotonic() + timeout
    while _prefetch_threads():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@contextlib.asynccontextmanager
async def _released(tts: _FakeTTS, task: asyncio.Task | None = None):
    """Whatever the test does (or fails to do), the held TTS request is released at the end and the
    worker is awaited (bounded), so a failed assertion cannot leak a blocked thread into the next
    test's thread-exit checks."""
    try:
        yield
    finally:
        tts.gate.set()  # first: nothing below can then wait on a held request
        task_hung = False
        if task is not None:
            try:  # on timeout wait_for cancels the gather and with it the task
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), TEARDOWN_S)
            except asyncio.TimeoutError:
                task_hung = True
        worker_exited = await _no_prefetch_threads(TEARDOWN_S)
        if task_hung:
            pytest.fail(f"teardown: the speaking task did not finish within {TEARDOWN_S} s")
        assert worker_exited, f"teardown: tts-prefetch worker still alive after {TEARDOWN_S} s"


@pytest.mark.asyncio
async def test_sentence_longer_than_the_live_cap_is_not_cut_inside_a_phrase() -> None:
    tts = _FakeTTS()
    await _orchestrator(tts, _Playback()).speak_verbatim("s", LONG)
    assert [text.strip() for text in tts.calls] == [
        "Trường hợp mặc thử mà chưa thấy vừa ý thì vẫn được đổi size thoải mái "
        "trong vòng mười lăm ngày đó luôn nha.",
        "Cảm ơn mọi người nhiều.",
    ]


@pytest.mark.asyncio
async def test_next_phrase_is_synthesized_while_the_current_one_plays() -> None:
    tts = _FakeTTS()
    overlapped: list[bool] = []
    # Hook runs as the FIRST phrase starts playing: a serial pipeline has not asked for the second
    # phrase yet, so the handshake would time out and record False.
    backend = _Playback(on_first=lambda: overlapped.append(tts.wait_begun(SECOND)))
    await _orchestrator(tts, backend).speak_verbatim("s", LONG)
    assert overlapped == [True]
    assert len(backend.windows) == 2


@pytest.mark.asyncio
async def test_order_is_kept_and_only_the_last_window_is_final() -> None:
    backend = _Playback()
    await _orchestrator(_FakeTTS(), backend).speak_verbatim("s", LONG)
    assert [w.text_span.strip()[:6] for w in backend.windows] == [FIRST, SECOND]
    assert [w.is_final for w in backend.windows] == [False, True]


@pytest.mark.asyncio
async def test_completed_prefetch_worker_exits_before_turn_returns(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    waits = []

    class RecordingPool(ThreadPoolExecutor):
        def shutdown(self, wait=True, **kwargs):
            waits.append(wait)
            return super().shutdown(wait=wait, **kwargs)

    monkeypatch.setattr("backend.application.render.orchestrator.ThreadPoolExecutor", RecordingPool)
    await _orchestrator(_FakeTTS(), _Playback()).speak_verbatim("settled", LONG)
    assert waits == [True]
    assert _prefetch_threads() == []


@pytest.mark.asyncio
async def test_tts_failure_in_the_prefetched_phrase_propagates() -> None:
    orch = _orchestrator(_FakeTTS(fail_on=SECOND), _Playback())
    with pytest.raises(TimeoutError):
        await orch.speak_verbatim("s", LONG)


@pytest.mark.asyncio
async def test_cancel_returns_promptly_while_the_lookahead_is_blocked() -> None:
    tts = _FakeTTS(block_on=SECOND)
    backend = _Playback(play_s=0.5)
    orch = _orchestrator(tts, backend)
    task = asyncio.create_task(orch.speak_verbatim("s", LONG))
    async with _released(tts, task):
        # Handshake: phrase 1 is playing AND the lookahead for phrase 2 really started and is held.
        assert await asyncio.to_thread(backend.playing.wait, 5)
        assert await asyncio.to_thread(tts.wait_begun, SECOND)
        assert not tts.has_ended(SECOND)
        cancelled_at = time.monotonic()
        await orch.cancel("s")
        await asyncio.wait_for(task, HELD_S)
        assert time.monotonic() - cancelled_at < PROMPT_S  # did not wait for the held request
        tts.gate.set()  # the hung request finally returns: its audio must be discarded
        assert await asyncio.to_thread(tts.wait_ended, SECOND)
        assert [w.text_span.strip()[:6] for w in backend.windows] == [FIRST]
        assert await _no_prefetch_threads()


@pytest.mark.asyncio
async def test_synthesis_error_propagates_without_requesting_the_next_phrase() -> None:
    tts = _FakeTTS(block_on=SECOND, fail_on=FIRST)
    orch = _orchestrator(tts, _Playback())
    async with _released(tts):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await orch.speak_verbatim("s", LONG)
        assert time.monotonic() - started < PROMPT_S
        assert tts.has_begun(FIRST) and not tts.has_begun(SECOND)  # phrase 2 never requested
        assert await _no_prefetch_threads()


@pytest.mark.asyncio
async def test_delivery_error_does_not_wait_for_the_blocked_lookahead() -> None:
    tts = _FakeTTS(block_on=SECOND)

    def reject() -> None:
        # Delivery of phrase 1 fails only once the lookahead for phrase 2 has really started.
        if not tts.wait_begun(SECOND):
            raise RuntimeError("the lookahead never started")
        raise ConnectionError("publication guard")

    orch = _orchestrator(tts, _Playback(on_first=reject))
    async with _released(tts):
        started = time.monotonic()
        with pytest.raises(ConnectionError):
            await orch.speak_verbatim("s", LONG)
        assert time.monotonic() - started < PROMPT_S
        assert tts.has_begun(SECOND) and not tts.has_ended(SECOND)  # still held in TTS
        tts.gate.set()
        assert await asyncio.to_thread(tts.wait_ended, SECOND)
        assert await _no_prefetch_threads()


@pytest.mark.asyncio
async def test_a_larger_configured_cap_does_not_enlarge_scripted_chunks() -> None:
    long_clause = "Mọi người " + "diện đi làm hay đi chơi đều rất hợp " * 8 + "nha."
    assert len(long_clause) > 200
    tts = _FakeTTS()
    await _orchestrator(tts, _Playback(), max_chars=500).speak_verbatim("s", long_clause)
    assert tts.calls  # not vacuous: the text really went through TTS
    assert len(tts.calls) >= 2  # it needed more than one chunk
    assert all(len(text) <= 200 for text in tts.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_index", [0, 1, 2])
async def test_typed_tts_failure_propagates_without_faking_completion(failed_index):
    from backend.application.clients.tts.self_hosted import TTSClientError

    error = TTSClientError("synthesis failed")

    class FailingTTS(_FakeTTS):
        def stream_audio(self, phrase, **kwargs):
            yield from super().stream_audio(phrase, **kwargs)
            if phrase.seq == failed_index:
                raise error

    tts = FailingTTS()
    backend = _Playback()
    text = LONG + " Thank you for joining us today."
    with pytest.raises(TTSClientError) as caught:
        await _orchestrator(tts, backend).speak_verbatim("s", text)
    assert caught.value is error
    assert len(tts.calls) == failed_index + 1
    assert len(backend.windows) == failed_index + 1  # Partial audio remains non-final.
    assert not any(w.is_final for w in backend.windows)
    assert await _no_prefetch_threads()


@pytest.mark.asyncio
async def test_all_phrases_failing_propagates_the_first_typed_error():
    from backend.application.clients.tts.self_hosted import TTSClientError

    error = TTSClientError("tts unavailable")
    attempted = []

    class FailingTTS(_FakeTTS):
        def stream_audio(self, phrase, **kwargs):
            attempted.append(phrase.text)
            raise error
            yield  # Keep the streaming interface without making any audio.

    backend = _Playback()
    with pytest.raises(TTSClientError) as caught:
        await _orchestrator(FailingTTS(), backend).speak_verbatim("s", LONG)
    assert caught.value is error
    assert len(attempted) == 1
    assert backend.windows == []
    assert await _no_prefetch_threads()


@pytest.mark.asyncio
async def test_lookahead_cap_fails_turn_and_blocks_a_second_worker(monkeypatch):
    from backend.application.clients.tts.self_hosted import TTSClientError

    monkeypatch.setattr(
        "backend.application.render.orchestrator.phrase_wait_budget_s", lambda _: 0.3
    )
    tts = _FakeTTS(block_on=FIRST)
    backend = _Playback()
    orch = _orchestrator(tts, backend)
    task = asyncio.create_task(orch.speak_verbatim("session-cap", LONG))
    async with _released(tts, task):
        assert await asyncio.to_thread(tts.wait_begun, FIRST)
        with pytest.raises(TTSClientError, match="lookahead deadline"):
            await asyncio.wait_for(asyncio.shield(task), 3)
        assert not tts.has_ended(FIRST)
        assert not tts.has_begun(SECOND)
        assert backend.windows == []
        with pytest.raises(TTSClientError, match="Previous TTS synthesis"):
            await asyncio.wait_for(orch.speak_verbatim("next-turn", LONG), 3)
        # Coordinator turns use a fresh orchestrator and may wrap the TTS engine.
        # Session-level exclusion must work even when neither object is reused.
        other_tts = _FakeTTS()
        with pytest.raises(TTSClientError, match="Previous TTS synthesis"):
            await _orchestrator(other_tts, _Playback()).speak_verbatim("session-cap", LONG)
        assert other_tts.calls == []
        assert len(_prefetch_threads()) == 1
        assert not tts.has_begun(SECOND)
        tts.gate.set()
        assert await asyncio.to_thread(tts.wait_ended, FIRST)
        assert await _no_prefetch_threads()
        # Once the abandoned worker exits, the same orchestrator can speak again.
        assert await orch.speak_verbatim("recovered", LONG) == LONG
        assert [w.is_final for w in backend.windows] == [False, True]


@pytest.mark.asyncio
async def test_approved_speech_rejection_still_aborts_remaining_phrases():
    from backend.application.script_authoring.approved_speech import SpeechRejected

    class RejectedTTS(_FakeTTS):
        def stream_audio(self, phrase, **kwargs):
            if phrase.seq == 0:
                raise SpeechRejected("altered_approved_chunk")
            yield from super().stream_audio(phrase, **kwargs)

    tts = RejectedTTS()
    backend = _Playback()
    with pytest.raises(SpeechRejected, match="altered_approved_chunk"):
        await _orchestrator(tts, backend).speak_verbatim("s", LONG)
    assert not tts.calls
    assert not backend.windows
