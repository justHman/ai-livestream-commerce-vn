"""Speech arbiter: explicit state machine over script sentences + Q&A (14.1-14.6, 14.9).

States (design Decision 17): ``SCRIPT_READY -> SCRIPT_SENTENCE_PLAYING``; at
the sentence boundary the arbiter revalidates pending Q&A (14.5) — none ->
next sentence, a winner -> ``QNA_PREPARING`` (the ONLY place the expensive
final generation runs, 14.6) -> ``QNA_PLAYING`` -> ``RESUME_BRIDGE`` -> the
exact next approved sentence (checkpoint preserved, 14.9). ``STOPPED`` and
``FAILED`` are terminal (operator stop vs internal failure).

Non-preemption (14.2): an active script sentence is never interrupted by
normal Q&A — the player awaits the full speech call; a mid-play ``update``
to the pending board only lands for the next boundary. Reducer processing
continues while playing (14.3): the board is a separate object the reducer
writes into concurrently; the arbiter only READS at boundaries.

``on_tick`` performs ONE transition per call, so the live coordinator
(cluster C14) drives it from its loop and tests drive it directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Protocol, runtime_checkable

from backend.application.live_runtime.pending_qa import PendingQaStore
from backend.application.live_runtime.qa_resolver import QaResolutionService
from backend.application.live_runtime.resume_bridge import build_resume_bridge, should_speak_bridge
from backend.application.live_runtime.sentence_speaker import SentenceCompletionError

__all__ = [
    "ArbiterConfig",
    "ArbiterState",
    "SpeechArbiter",
    "SpeechTextLike",
]


class ArbiterState(StrEnum):
    """Explicit arbiter FSM states (design Decision 17)."""

    SCRIPT_READY = "script_ready"
    SCRIPT_SENTENCE_PLAYING = "script_sentence_playing"
    QNA_PENDING = "qna_pending"
    QNA_PREPARING = "qna_preparing"
    QNA_PLAYING = "qna_playing"
    RESUME_BRIDGE = "resume_bridge"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ArbiterConfig:
    """Tunables for the arbiter (design Decisions 17-19)."""

    prefetch_stable_evidence: bool = True
    speak_resume_bridge: bool = True
    resume_bridge_template: str = "Rồi, em tiếp tục với {product} nhé."
    max_qa_speech_length: int = 400
    bridge_topic: str = "sản phẩm"


@runtime_checkable
class SpeechTextLike(Protocol):
    """Minimal speak hook (structural stand-in for the speech service)."""

    async def speak_sentence(self, session_id: str, text: str) -> str: ...


class _CursorLike(Protocol):
    """Structural cursor the arbiter drives (``CursorLike`` shape + position)."""

    @property
    def finished(self) -> bool: ...

    def current_sentence(self) -> Any: ...

    def complete_current(self) -> None: ...

    def position(self) -> Any: ...

    def checkpoint_next_before_qa(self) -> None: ...


class SpeechArbiter:
    """Drives script sentences and interleaves pending Q&A at boundaries."""

    def __init__(
        self,
        *,
        cursor: Any,
        player: Any,
        pending: PendingQaStore,
        resolver: QaResolutionService,
        speech: SpeechTextLike,
        reducer_notifier: Callable[[], bool] | None = None,
        config: ArbiterConfig | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._cursor = cursor
        self._player = player
        self._pending = pending
        self._resolver = resolver
        self._speech = speech
        self._reducer_notifier = reducer_notifier
        self._config = config or ArbiterConfig()
        self._now = now or (lambda: 0.0)
        self._state = ArbiterState.SCRIPT_READY
        self._qa_speech_text = ""
        self._qa_product_id: str | None = None
        self._history: list[tuple[ArbiterState, float]] = [(self._state, self._now())]

    @property
    def state(self) -> ArbiterState:
        return self._state

    def state_history(self) -> list[tuple[ArbiterState, float]]:
        return list(self._history)

    def _transition(self, state: ArbiterState) -> None:
        self._state = state
        self._history.append((state, self._now()))

    def _drain_reducer(self) -> None:
        """Pull latest candidates from the reducer at a safe point (14.3)."""
        if self._reducer_notifier is not None and self._reducer_notifier():
            pass

    async def _speak(self, session_id: str, text: str) -> None:
        """Speak one deterministic turn; ``max_qa_speech_length`` bounds it."""
        if len(text) > self._config.max_qa_speech_length:
            text = text[: self._config.max_qa_speech_length]
        # The player owns cursor advancement for script sentences; Q&A turns
        # and the resume bridge never touch the cursor (14.9 checkpoint).
        await self._speech.speak_sentence(session_id, text)

    async def on_tick(self, session_id: str, stale_at: float | None = None) -> ArbiterState:
        """Advance exactly one transition (re-entrant safe; coordinator drives).

        ``stale_at`` overrides the clock only for the pending-board read at
        this boundary (activeness revalidation test hook).
        """
        self._drain_reducer()
        if self._state == ArbiterState.SCRIPT_READY:
            if self._cursor.finished:
                self._transition(ArbiterState.STOPPED)
                return self._state
            self._transition(ArbiterState.SCRIPT_SENTENCE_PLAYING)
            try:
                await self._player.play_current(session_id)
            except SentenceCompletionError:
                self._transition(ArbiterState.FAILED)
                return self._state
            self._drain_reducer()
            winner = self._pending.pending_winner(stale_at)
            if winner is None or not self._pending.is_eligible(winner, stale_at):
                if self._cursor.finished:
                    self._transition(ArbiterState.STOPPED)
                else:
                    self._transition(ArbiterState.SCRIPT_READY)
                return self._state
            self._transition(ArbiterState.QNA_PENDING)
            return self._state
        if self._state == ArbiterState.QNA_PENDING:
            self._transition(ArbiterState.QNA_PREPARING)
            return self._state
        if self._state == ArbiterState.QNA_PREPARING:
            winner = self._pending.pending_winner(self._now())
            if winner is None or not self._pending.is_eligible(winner, self._now()):
                self._transition(ArbiterState.SCRIPT_READY)
                return self._state
            if self._config.prefetch_stable_evidence:
                self._resolver.prefetch_stable_evidence(winner)
            resolution = await self._resolver.resolve_qa(winner)
            self._pending.mark_answered(winner.cluster_id, self._now())
            if resolution.kind != "answer" or not resolution.speech_text:
                self._qa_speech_text = ""
                self._transition(ArbiterState.SCRIPT_READY)
                return self._state
            self._qa_speech_text = resolution.speech_text
            # The winner is a PendingQaCandidate wrapping the envelope; tests
            # also pass raw envelopes, so unwrap defensively.
            envelope = getattr(winner, "envelope", winner)
            self._qa_product_id = (
                envelope.resolved_product_ids[0] if envelope.resolved_product_ids else None
            )
            self._transition(ArbiterState.QNA_PLAYING)
            return self._state
        if self._state == ArbiterState.QNA_PLAYING:
            if self._qa_speech_text:
                await self._speak(session_id, self._qa_speech_text)
            self._transition(ArbiterState.RESUME_BRIDGE)
            return self._state
        if self._state == ArbiterState.RESUME_BRIDGE:
            position = self._cursor.position() if hasattr(self._cursor, "position") else None
            position_product = (
                getattr(position, "product_id", None) if position is not None else None
            )
            current_product = position_product or self._config.bridge_topic
            if should_speak_bridge(
                config_enabled=self._config.speak_resume_bridge,
                script_finished=self._cursor.finished,
                previous_product=self._qa_product_id,
                current_product=current_product,
            ):
                bridge = build_resume_bridge(
                    current_product, template=self._config.resume_bridge_template
                )
                await self._speak(session_id, bridge)
            self._transition(ArbiterState.SCRIPT_READY)
            return self._state
        return self._state

    async def hard_stop(self, session_id: str) -> None:
        """Operator hard interrupt (separate control-plane op, task 14.10).

        Cancels active speech when the player's speech service supports it,
        then enters the terminal STOPPED state and clears the pending board.
        Never called from the normal tick path.
        """
        cancel = getattr(self._speech, "cancel", None)
        if callable(cancel):
            try:
                await cancel(session_id)
            except Exception:
                pass
        self._pending.clear()
        self._transition(ArbiterState.STOPPED)
