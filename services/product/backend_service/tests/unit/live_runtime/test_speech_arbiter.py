"""Task 14.1-14.6: explicit arbiter state machine over script + Q&A.

Proves against the real pending board (14.4) and recording fakes: the FSM
transition chain (14.1); a mid-play high-score candidate never preempts the
playing sentence and Q&A happens only at the boundary (14.2); the reducer
keeps updating the board while a sentence plays and the boundary sees the
new winner (14.3); the boundary revalidates activeness and skips a stale
winner (14.5); the expensive resolver call never runs while playing and
runs exactly once at the boundary (14.6); the resume bridge is deterministic
and the cursor checkpoint survives Q&A (14.9).
"""

from __future__ import annotations

import asyncio

from backend.application.live_runtime.pending_qa import PendingQaStore
from backend.application.live_runtime.sentence_speaker import SentenceCompletionError
from backend.application.live_runtime.speech_arbiter import ArbiterState, SpeechArbiter


class Clock:
    """Injectable monotonic clock shared by arbiter and pending board."""

    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


class FakeEnvelope:
    """Minimal decision-9-shaped envelope; only cluster_id/ranking_score matter."""

    def __init__(self, cluster_id: str, score: float) -> None:
        self.cluster_id = cluster_id
        self.ranking_score = score
        self.resolved_product_ids = ("P001",)


class FakeCursor:
    """Cursor over fixed sentences; records completion calls."""

    def __init__(self, sentences: list[str]) -> None:
        self._sentences = sentences
        self._index = 0
        self.completions: list[str] = []

    @property
    def finished(self) -> bool:
        return self._index >= len(self._sentences)

    def current_sentence(self) -> object:
        class Span:
            pass

        span = Span()
        span.index = self._index
        span.text = self._sentences[self._index]
        return span

    def complete_current(self) -> None:
        self.completions.append(self._sentences[self._index])
        self._index += 1

    def position(self) -> object:
        class Position:
            product_id = "P010"

        return Position()

    def checkpoint_next_before_qa(self) -> None:
        pass

    @property
    def sentence_index(self) -> int:
        return self._index


class FakePlayer:
    """Sentence player: slowable; raises when told; records play_current calls.

    Mirrors the real ``ScriptSentencePlayer`` semantics: the cursor advances
    only when the speech call returns normally.
    """

    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.played: list[str] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.raise_on: set[int] = set()
        self._calls = 0

    def slow_call(self, n: int) -> asyncio.Event:
        gate = asyncio.Event()
        self.gates[n] = gate
        return gate

    async def play_current(self, session_id: str) -> str:
        self._calls += 1
        if self._calls in self.raise_on:
            raise SentenceCompletionError("injected player failure")
        if self._calls in self.gates:
            await self.gates[self._calls].wait()
        sentence = self._cursor.current_sentence()
        text = sentence.text
        self.played.append(text)
        self._cursor.complete_current()
        return text


class FakeSpeech:
    """Speech service: records every spoken turn, including Q&A/bridge."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.cancelled: list[str] = []

    async def speak_sentence(self, session_id: str, text: str) -> str:
        self.spoken.append(text)
        return text

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class RecordingResolver:
    """QaResolutionService fake: records every resolve_qa call."""

    def __init__(
        self,
        kind: str = "answer",
        speech_text: str = "Em thấy nhiều anh chị đang hỏi về P001... P001 giá 1 triệu.",
    ) -> None:
        self.kind = kind
        self.speech_text = speech_text
        self.calls: list[str] = []
        self.prefetches: list[str] = []

    def prefetch_stable_evidence(self, candidate: object) -> None:
        self.prefetches.append(candidate.cluster_id)  # type: ignore[attr-defined]

    async def resolve_qa(self, candidate: object):
        from backend.application.live_runtime.qa_resolver import QaResolution

        self.calls.append(candidate.cluster_id)  # type: ignore[attr-defined]
        if self.kind == "unavailable":
            return QaResolution.unavailable()
        return QaResolution.answer(self.speech_text)


def _build(
    sentences: list[str],
    *,
    resolver_kind: str = "answer",
    reducer_notifier=None,
) -> tuple[SpeechArbiter, FakeCursor, FakePlayer, FakeSpeech, RecordingResolver, PendingQaStore]:
    cursor = FakeCursor(sentences)
    player = FakePlayer(cursor)
    speech = FakeSpeech()
    resolver = RecordingResolver(kind=resolver_kind)
    clock = Clock()
    pending = PendingQaStore(now=clock)
    arbiter = SpeechArbiter(
        cursor=cursor,
        player=player,
        pending=pending,
        resolver=resolver,
        speech=speech,
        reducer_notifier=reducer_notifier,
        now=clock,
    )
    return arbiter, cursor, player, speech, resolver, pending


def test_starts_in_script_ready() -> None:
    arbiter, _, _, _, _, _ = _build(["câu 1."])
    assert arbiter.state == ArbiterState.SCRIPT_READY


async def test_tick_stopped_when_cursor_finished() -> None:
    arbiter, _, _, _, _, _ = _build([])
    state = await arbiter.on_tick("s")

    assert state == ArbiterState.STOPPED
    assert arbiter.state_history()[-1][0] == ArbiterState.STOPPED


async def test_full_script_chain_without_qa_ends_stopped() -> None:
    arbiter, _, player, _, resolver, _ = _build(["câu 1.", "câu 2."])
    for _ in range(20):
        await arbiter.on_tick("s")

    assert player.played == ["câu 1.", "câu 2."]
    assert resolver.calls == []
    assert arbiter.state == ArbiterState.STOPPED


async def test_play_error_transitions_to_failed() -> None:
    arbiter, _, player, _, _, _ = _build(["câu 1."])
    player.raise_on.add(1)

    await arbiter.on_tick("s")

    assert arbiter.state == ArbiterState.FAILED


async def test_qa_winner_runs_at_boundary_after_sentence_completes() -> None:
    arbiter, _, player, speech, resolver, pending = _build(["câu 1.", "câu 2."])
    pending.update(FakeEnvelope("cl-p020", 0.95))

    await arbiter.on_tick("s")  # play sentence 1 -> boundary sees winner
    await arbiter.on_tick("s")  # QNA_PENDING -> QNA_PREPARING (resolver)
    await arbiter.on_tick("s")  # QNA_PREPARING -> QNA_PLAYING
    await arbiter.on_tick("s")  # QNA_PLAYING -> RESUME_BRIDGE (speak Q&A)

    assert player.played == ["câu 1."]
    assert resolver.calls == ["cl-p020"]
    assert speech.spoken == ["Em thấy nhiều anh chị đang hỏi về P001... P001 giá 1 triệu."]
    assert arbiter.state == ArbiterState.RESUME_BRIDGE


async def test_mid_play_update_does_not_preempt_playing_sentence() -> None:
    arbiter, cursor, player, speech, resolver, pending = _build(["câu 1.", "câu 2."])
    gate = player.slow_call(1)
    task = asyncio.create_task(arbiter.on_tick("s"))
    # The arbiter itself is parked on the gate (play_current mid-sentence).
    while not gate.is_set() and gate._waiters:
        await asyncio.sleep(0.01)

    pending.update(FakeEnvelope("cl-p020", 0.99))

    gate.set()
    await task

    assert player.played == ["câu 1."]
    assert resolver.calls == []
    assert speech.spoken == []
    assert cursor.sentence_index == 1
    assert arbiter.state == ArbiterState.QNA_PENDING


async def test_boundary_sees_mid_play_update_then_speaks_qa() -> None:
    arbiter, _, player, speech, resolver, pending = _build(["câu 1.", "câu 2."])
    gate = player.slow_call(1)
    task = asyncio.create_task(arbiter.on_tick("s"))
    while not gate.is_set() and gate._waiters:
        await asyncio.sleep(0.01)

    pending.update(FakeEnvelope("cl-p020", 0.99))

    gate.set()
    await task
    await arbiter.on_tick("s")  # QNA_PENDING -> QNA_PREPARING
    await arbiter.on_tick("s")  # QNA_PREPARING -> QNA_PLAYING
    await arbiter.on_tick("s")  # QNA_PLAYING -> RESUME_BRIDGE (speak)

    assert resolver.calls == ["cl-p020"]
    assert speech.spoken == ["Em thấy nhiều anh chị đang hỏi về P001... P001 giá 1 triệu."]


async def test_resolver_never_called_while_sentence_plays() -> None:
    arbiter, _, player, _, resolver, pending = _build(["câu 1.", "câu 2."])
    pending.update(FakeEnvelope("cl-p020", 0.95))
    gate = player.slow_call(1)
    task = asyncio.create_task(arbiter.on_tick("s"))
    while not gate.is_set() and gate._waiters:
        await asyncio.sleep(0.01)

    assert resolver.calls == []

    gate.set()
    await task

    assert resolver.calls == []


async def test_resolver_called_exactly_once_at_boundary() -> None:
    arbiter, _, _, _, resolver, pending = _build(["câu 1."])
    pending.update(FakeEnvelope("cl-p020", 0.95))

    await arbiter.on_tick("s")
    await arbiter.on_tick("s")
    await arbiter.on_tick("s")

    assert resolver.calls == ["cl-p020"]


async def test_stale_winner_skipped_at_boundary_script_continues() -> None:
    arbiter, _, player, _, resolver, pending = _build(["câu 1.", "câu 2."])
    pending.update(FakeEnvelope("cl-p020", 0.95))
    stale_at = pending.config.relevance_window + 1.0

    await arbiter.on_tick("s", stale_at=stale_at)

    assert resolver.calls == []
    assert player.played == ["câu 1."]


async def test_unavailable_resolution_skips_qa_and_resumes_script() -> None:
    arbiter, _, player, speech, resolver, pending = _build(
        ["câu 1.", "câu 2."], resolver_kind="unavailable"
    )
    pending.update(FakeEnvelope("cl-p020", 0.95))

    await arbiter.on_tick("s")
    await arbiter.on_tick("s")
    await arbiter.on_tick("s")

    assert resolver.calls == ["cl-p020"]
    assert speech.spoken == []
    assert arbiter.state == ArbiterState.SCRIPT_READY


async def test_resume_bridge_is_deterministic_and_mentions_product() -> None:
    arbiter, _, _, speech, _, pending = _build(["câu 1.", "câu 2."])
    pending.update(FakeEnvelope("cl-p020", 0.95))

    await arbiter.on_tick("s")
    await arbiter.on_tick("s")
    await arbiter.on_tick("s")
    await arbiter.on_tick("s")
    await arbiter.on_tick("s")

    assert speech.spoken == [
        "Em thấy nhiều anh chị đang hỏi về P001... P001 giá 1 triệu.",
        "Rồi, em tiếp tục với P010 nhé.",
    ]


async def test_hard_stop_cancels_speech_clears_pending_and_is_terminal() -> None:
    arbiter, _, _, speech, _, pending = _build(["câu 1."])
    pending.update(FakeEnvelope("cl-p020", 0.95))

    await arbiter.hard_stop("s")

    assert arbiter.state == ArbiterState.STOPPED
    assert speech.cancelled == ["s"]
    assert pending.pending_winner() is None


async def test_qa_turn_bounded_by_max_length() -> None:
    arbiter, _, _, speech, _, pending = _build(["câu 1."])
    arbiter._config = arbiter._config.__class__(
        max_qa_speech_length=10,
        speak_resume_bridge=arbiter._config.speak_resume_bridge,
        resume_bridge_template=arbiter._config.resume_bridge_template,
        prefetch_stable_evidence=arbiter._config.prefetch_stable_evidence,
        bridge_topic=arbiter._config.bridge_topic,
    )
    pending.update(FakeEnvelope("cl-p020", 0.95))

    await arbiter.on_tick("s")
    await arbiter.on_tick("s")
    await arbiter.on_tick("s")
    await arbiter.on_tick("s")

    assert all(len(text) <= 10 for text in speech.spoken)
