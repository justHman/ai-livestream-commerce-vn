"""Task 14.10: hard interrupt is a DISTINCT control-plane path.

Proves: (a) a hard interrupt can never be created through the pending-Q&A
board (no path from ``PendingQaStore.update`` to STOPPED); (b) the arbiter's
``hard_stop`` transitions to STOPPED and cancels active speech mid-sentence;
(c) after STOPPED, normal ticks are no-ops; (d) the normal Q&A flow never
calls the hard-stop path - only the operator/control-plane entry does; (e)
the canonical ``POST /sessions/{id}/interrupt`` route delegates to the
coordinator/orchestrator cancel path and never goes through pending-Q&A.
"""

from __future__ import annotations

import inspect

from backend.application.live_runtime.hard_interrupt import (
    HARD_INTERRUPT_OPERATION,
    HardInterruptService,
    assert_hard_interrupt_never_from_pending_qa,
    is_hard_interrupt,
)
from backend.application.live_runtime.pending_qa import PendingQaStore, QaHysteresisConfig
from backend.application.live_runtime.speech_arbiter import ArbiterState, SpeechArbiter


class _FakeEnvelope:
    def __init__(self, cluster_id: str, score: float) -> None:
        self.cluster_id = cluster_id
        self.ranking_score = score
        self.resolved_product_ids = ("P001",)


class _FakeCursor:
    def __init__(self, sentences: list[str]) -> None:
        self._sentences = sentences
        self._index = 0

    @property
    def finished(self) -> bool:
        return self._index >= len(self._sentences)

    def current_sentence(self):
        span = type("Span", (), {"index": self._index, "text": self._sentences[self._index]})()
        return span

    def complete_current(self) -> None:
        self._index += 1

    def position(self):
        return type("Position", (), {"product_id": "P010"})()


class _FakePlayer:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    async def play_current(self, session_id: str) -> str:
        text = self._cursor.current_sentence().text
        self._cursor.complete_current()
        return text


class _RecordingSpeech:
    """Speech fake with a cancel seam; records every cancel."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.cancelled: list[str] = []

    async def speak_sentence(self, session_id: str, text: str) -> str:
        self.spoken.append(text)
        return text

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class _AnsweringResolver:
    async def resolve_qa(self, candidate):
        from backend.application.live_runtime.qa_resolver import QaResolution

        return QaResolution.answer("Câu trả lời P020.", lead_in="")

    def prefetch_stable_evidence(self, candidate) -> None:
        pass


def _arbiter(sentences: list[str]):
    cursor = _FakeCursor(sentences)
    speech = _RecordingSpeech()
    arbiter = SpeechArbiter(
        cursor=cursor,
        player=_FakePlayer(cursor),
        pending=PendingQaStore(),
        resolver=_AnsweringResolver(),
        speech=speech,
    )
    return arbiter, cursor, speech


# --- (a) hard interrupt is never a pending-Q&A candidate ----------------------


def test_hard_interrupt_never_created_through_pending_qa() -> None:
    pending = PendingQaStore(config=QaHysteresisConfig())
    pending.update(_FakeEnvelope("cl-viewer-qa", 0.9))

    assert HARD_INTERRUPT_OPERATION not in [c.cluster_id for c in pending._candidates.values()]
    assert_hard_interrupt_never_from_pending_qa(pending)


def test_guard_flags_hard_interrupt_candidate() -> None:
    pending = PendingQaStore()
    pending._candidates[HARD_INTERRUPT_OPERATION] = pending.update(
        _FakeEnvelope(HARD_INTERRUPT_OPERATION, 1.0)
    )

    try:
        assert_hard_interrupt_never_from_pending_qa(pending)
        raise AssertionError("guard must reject a hard-interrupt candidate")
    except RuntimeError:
        pass


# --- (b) hard_stop cancels active speech and enters STOPPED ------------------


async def test_hard_stop_cancels_speech_and_enters_stopped() -> None:
    arbiter, _, speech = _arbiter(["Câu một.", "Câu hai."])
    await arbiter.on_tick("s")  # sentence 1 plays

    await arbiter.hard_stop("s")

    assert arbiter.state == ArbiterState.STOPPED
    assert speech.cancelled == ["s"]


# --- (c) after STOPPED normal ticks are no-ops -------------------------------


async def test_ticks_after_stopped_are_noops() -> None:
    arbiter, cursor, _ = _arbiter(["Câu một."])
    await arbiter.hard_stop("s")

    state = await arbiter.on_tick("s")

    assert state == ArbiterState.STOPPED
    assert cursor._index == 0  # no sentence started, no advancement


# --- (d) the normal Q&A flow never calls hard_stop ---------------------------


async def test_normal_qa_flow_never_invokes_hard_stop() -> None:
    arbiter, _, speech = _arbiter(["Câu một."])
    pending = arbiter._pending
    pending.update(_FakeEnvelope("cl-p020", 0.95))

    for _ in range(6):
        await arbiter.on_tick("s")

    # The Q&A turn spoke but the arbiter never cancelled speech: hard_stop
    # is not on the normal tick path.
    assert speech.cancelled == []
    assert speech.spoken != []


async def test_qa_scheduling_operation_is_not_hard_interrupt() -> None:
    for op in ("schedule_qa", "resolve_qa", "boundary_tick"):
        assert is_hard_interrupt(op) is False
    assert is_hard_interrupt(HARD_INTERRUPT_OPERATION) is True


# --- (e) canonical control-plane route delegates to cancel path --------------


def test_interrupt_route_is_distinct_control_plane_operation() -> None:
    """The route handler exists and delegates to coordinator/orchestrator cancel.

    Static proof: the canonical route calls the coordinator's interrupt (which
    cancels the orchestrator + backend) or the orchestrator cancel directly -
    it never feeds the pending-Q&A board.
    """
    from backend.api.v1.sessions import sessions_interrupt

    source = inspect.getsource(sessions_interrupt)
    assert "coordinator" in source and "interrupt" in source
    assert "orchestrators" in source and "cancel" in source
    assert "pending" not in source and "PendingQaStore" not in source


async def test_hard_interrupt_service_cancels_via_speech_seam() -> None:
    speech = _RecordingSpeech()
    service = HardInterruptService(speech)

    await service.interrupt("sess-1")

    assert speech.cancelled == ["sess-1"]
