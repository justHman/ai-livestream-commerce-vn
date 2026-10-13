"""Scripted (verbatim) turns: phrase boundaries and synthesis overlap.

Live test 2026-10-13: speech paused inside a word pair ("thoải | mái") and lagged between chunks.
Two causes in our pipeline: the 80-char live cap cut sentences at whitespace, and the next phrase
was synthesized only after the current one had been (almost) played out.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Iterator

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


class _NoLLM(LLMEngine):
    name = "no-llm"

    @classmethod
    def from_config(cls, cfg: dict) -> "_NoLLM":  # pragma: no cover
        return cls()

    def generate(self, req):  # pragma: no cover
        raise RuntimeError("verbatim never calls the LLM")


class _TimedTTS:
    """Each call takes ``synth_s`` and yields one 200 ms window; records when each phrase ran."""

    name = "timed-tts"

    def __init__(self, synth_s: float = 0.0) -> None:
        self.synth_s = synth_s
        self.calls: list[tuple[str, float, float]] = []

    def stream_audio(self, phrase, *, session_id="", utterance_id="", **_) -> Iterator[AudioWindow]:
        start = time.monotonic()
        time.sleep(self.synth_s)
        self.calls.append((phrase.text, start, time.monotonic()))
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


class _SlowPlayback(MockRenderBackend):
    """stream_audio blocks like a real avatar: until the audio has (almost) played."""

    def __init__(self, play_s: float) -> None:
        super().__init__()
        self.play_s = play_s
        self.windows: list[AudioWindow] = []
        self.delivery: list[tuple[float, float]] = []

    def stream_audio(self, session_id, audio_window):
        start = time.monotonic()
        time.sleep(self.play_s)
        self.windows.append(audio_window)
        self.delivery.append((start, time.monotonic()))
        return iter(())


def _orchestrator(tts, backend) -> StreamOrchestrator:
    backend.start(StartOptions())
    return StreamOrchestrator(
        llm=_NoLLM(),
        tts=tts,
        backend=backend,
        queue=BoundedVideoQueue(max_size=20),
        metrics=CoordinatorMetrics(),
        fixed_config=FixedChunkPolicyConfig(min_chars=12, target_chars=40, max_chars=80),
    )


@pytest.mark.asyncio
async def test_sentence_longer_than_the_live_cap_is_not_cut_inside_a_phrase() -> None:
    tts = _TimedTTS()
    orch = _orchestrator(tts, _SlowPlayback(0.0))
    await orch.speak_verbatim("s", LONG)
    spoken = [text.strip() for text, _, _ in tts.calls]
    assert spoken == [
        "Trường hợp mặc thử mà chưa thấy vừa ý thì vẫn được đổi size thoải mái "
        "trong vòng mười lăm ngày đó luôn nha.",
        "Cảm ơn mọi người nhiều.",
    ]


@pytest.mark.asyncio
async def test_next_phrase_is_synthesized_while_the_current_one_plays() -> None:
    tts, backend = _TimedTTS(synth_s=0.15), _SlowPlayback(0.4)
    orch = _orchestrator(tts, backend)
    await orch.speak_verbatim("s", LONG)
    assert len(tts.calls) == 2 and len(backend.delivery) == 2
    second_synth_start = tts.calls[1][1]
    first_delivery_end = backend.delivery[0][1]
    # Serial pipeline: the second synthesis would start only after the first delivery ended.
    assert second_synth_start < first_delivery_end


@pytest.mark.asyncio
async def test_order_is_kept_and_only_the_last_window_is_final() -> None:
    backend = _SlowPlayback(0.0)
    orch = _orchestrator(_TimedTTS(synth_s=0.02), backend)
    await orch.speak_verbatim("s", LONG)
    assert [w.text_span.strip()[:6] for w in backend.windows] == ["Trường", "Cảm ơn"]
    assert [w.is_final for w in backend.windows] == [False, True]


@pytest.mark.asyncio
async def test_tts_failure_in_the_prefetched_phrase_propagates() -> None:
    class _Boom(_TimedTTS):
        def stream_audio(self, phrase, **kw):
            if phrase.text.strip().startswith("Cảm"):
                raise TimeoutError("tts down")
            yield from super().stream_audio(phrase, **kw)

    orch = _orchestrator(_Boom(), _SlowPlayback(0.0))
    with pytest.raises(TimeoutError):
        await orch.speak_verbatim("s", LONG)


class _GatedTTS(_TimedTTS):
    """The phrase starting with ``block_on`` waits on ``gate`` (a hung TTS request)."""

    def __init__(self, block_on: str, *, fail_on: str = "") -> None:
        super().__init__()
        self.block_on, self.fail_on = block_on, fail_on
        self.gate = threading.Event()
        self.started: list[str] = []

    def stream_audio(self, phrase, **kw):
        self.started.append(phrase.text.strip()[:6])
        if self.fail_on and phrase.text.strip().startswith(self.fail_on):
            raise TimeoutError("tts down")
        if phrase.text.strip().startswith(self.block_on):
            self.gate.wait(5)
        yield from super().stream_audio(phrase, **kw)


def _prefetch_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("tts-prefetch")]


@pytest.mark.asyncio
async def test_cancel_returns_promptly_while_the_lookahead_is_blocked() -> None:
    tts = _GatedTTS(block_on="Cảm")
    backend = _SlowPlayback(0.5)
    orch = _orchestrator(tts, backend)
    started = time.monotonic()
    task = asyncio.create_task(orch.speak_verbatim("s", LONG))
    await asyncio.sleep(0.2)  # phrase 1 is playing, phrase 2 is blocked in TTS
    assert tts.started == ["Trường", "Cảm ơn"]
    await orch.cancel("s")
    await asyncio.wait_for(task, 3)
    assert time.monotonic() - started < 2  # the gate holds the lookahead for 5 s
    tts.gate.set()  # the hung request finally returns: its audio must be discarded
    await asyncio.sleep(0.3)
    assert [w.text_span.strip()[:6] for w in backend.windows] == ["Trường"]
    assert _prefetch_threads() == []  # no worker left behind


@pytest.mark.asyncio
async def test_synthesis_error_propagates_without_requesting_the_next_phrase() -> None:
    tts = _GatedTTS(block_on="Cảm", fail_on="Trường")
    orch = _orchestrator(tts, _SlowPlayback(0.0))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await orch.speak_verbatim("s", LONG)
    assert time.monotonic() - started < 2
    assert tts.started == ["Trường"]  # phrase 2 was never requested
    assert _prefetch_threads() == []


@pytest.mark.asyncio
async def test_delivery_error_does_not_wait_for_the_blocked_lookahead() -> None:
    tts = _GatedTTS(block_on="Cảm")

    class _Reject(_SlowPlayback):
        def stream_audio(self, session_id, audio_window):
            raise ConnectionError("publication guard")

    orch = _orchestrator(tts, _Reject(0.0))
    started = time.monotonic()
    with pytest.raises(ConnectionError):
        await orch.speak_verbatim("s", LONG)
    assert time.monotonic() - started < 2  # phrase 2 is still blocked in TTS
    tts.gate.set()
    await asyncio.sleep(0.3)
    assert _prefetch_threads() == []


@pytest.mark.asyncio
async def test_a_larger_configured_cap_does_not_enlarge_scripted_chunks() -> None:
    long_clause = "Mọi người " + "diện đi làm hay đi chơi đều rất hợp " * 8 + "nha."
    tts = _TimedTTS()
    orch = StreamOrchestrator(
        llm=_NoLLM(),
        tts=tts,
        backend=_SlowPlayback(0.0),
        queue=BoundedVideoQueue(max_size=20),
        metrics=CoordinatorMetrics(),
        fixed_config=FixedChunkPolicyConfig(min_chars=12, target_chars=40, max_chars=500),
    )
    orch._backend.start(StartOptions())
    await orch.speak_verbatim("s", long_clause)
    assert len(long_clause) > 200
    assert all(len(text) <= 200 for text, _, _ in tts.calls)
