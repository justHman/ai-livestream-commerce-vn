"""Source-agnostic speech text chunker state machine (OpenSpec
adaptive-speech-text-chunking).

Coalesces arbitrary text fragments into phrase-sized ``TextChunk`` values
under an injected segmentation strategy (``fixed`` or ``adaptive_vi``).

Behavior contract:
- ``feed()`` appends arbitrary text, scans the accumulated buffer for
  boundaries, and drains ALL completed phrases — one call may return many
  chunks. Every automatic non-final chunk is ``<= max_chars``; oversized
  buffers are split at the cap with remainder retained.
- ``flush(reason=...)`` commits the current buffer as one non-final chunk
  (sub-min allowed, explicit caller action).
- ``finalize()`` emits the remaining buffer as the final chunk; an empty
  buffer returns ``[]`` — completion with no textual remainder is handled
  by orchestration finality.
- The chunker owns NO timeout knob: realtime waiting/deadlines belong to
  streaming orchestration. The orchestrator computes the deadline from
  ``buffer_started_at``/``buffer_age_ms`` (both exposed) and calls
  ``flush(reason=LATENCY_DEADLINE)`` explicitly when it expires. Buffer age
  starts only when the first non-empty fragment enters an empty buffer —
  long TTFT never ages an empty buffer.

Strategies:
- ``fixed``: the historical deterministic rule (first punctuation with a
  ``[min, max]`` prefix, then last whitespace at/before the cap, then the
  exact cap; ``target_chars`` fixes the finalize fallback boundary).
  Behavior is byte-for-byte unchanged and it doubles as the explicit
  runtime rollback.
- ``adaptive_vi``: deterministic Vietnamese boundary selection. During the
  drain loop the strategy commits the earliest strong (paragraph/sentence),
  non-protected boundary at/after ``min_chars`` — a function of the
  accumulated prefix alone, so segmentation is invariant to how the input
  was fragmented. Weak boundaries (clause/comma/cue/whitespace) are never
  committed prematurely by default; runtime hints may shrink the soft
  duration target, which allows committing the earliest weak boundary whose
  head fits the shrunk target (startup/starvation), and shift scoring under
  hard-cap pressure — the hard invariants (strong commitment, cap,
  ``min_chars``/``max_chars``, protection) are unchanged. The hard cap always
  wins when no natural boundary exists at or before ``max_chars``. If
  adaptive analysis fails (exception or non-finite estimate), the utterance
  fails closed to the ``fixed`` strategy and every subsequent chunk is
  stamped ``fixed_fallback`` — text is never dropped or reordered.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .duration import SpeechDurationEstimator
from .policy import (
    AdaptiveViPolicyConfig,
    AdaptiveViPolicyStrategy,
    ChunkingPolicy,
    FixedChunkPolicyConfig,
    FixedPolicyStrategy,
)
from .telemetry import ChunkTelemetry, TelemetryCollector
from .types import ChunkDecisionReason, ChunkPolicy, RuntimeHints, TextChunk

__all__ = ["TextChunk", "TextChunker"]


class TextChunker:
    """Source-agnostic speech text chunker state machine.

    ONE chunker hosts the shared buffer/emit/telemetry state; the injected
    segmentation strategy (``fixed`` or ``adaptive_vi``) owns only the
    boundary decisions. Stateful: holds a text buffer, the monotonic time
    the current buffer first received text (``buffer_started_at``), and a
    monotonically increasing ``seq``. No timers or threads: realtime
    waiting belongs to orchestration.
    """

    def __init__(
        self,
        session_id: str,
        utterance_id: str,
        min_chars: int = 12,
        target_chars: int = 40,
        max_chars: int = 80,
        clock: Optional[Callable[[], float]] = None,
        policy: str | ChunkPolicy = "fixed",
        estimator: Optional[SpeechDurationEstimator] = None,
        telemetry: Optional[TelemetryCollector] = None,
        *,
        fixed_config: Optional[FixedChunkPolicyConfig] = None,
        adaptive_config: Optional[AdaptiveViPolicyConfig] = None,
    ) -> None:
        if isinstance(policy, ChunkPolicy):
            self.policy = policy
        else:
            try:
                self.policy = ChunkPolicy(policy)
            except ValueError:
                raise ValueError(f"unknown policy {policy!r}") from None
        self.session_id = session_id
        self.utterance_id = utterance_id
        self.min_chars = min_chars
        self.target_chars = target_chars
        self.max_chars = max_chars
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._estimator = (
            estimator
            if estimator is not None
            else SpeechDurationEstimator()
            if self.policy == ChunkPolicy.ADAPTIVE_VI
            else None
        )
        # The strategy is chosen at construction — no monolithic mode-switch
        # in the chunker body. The fixed strategy is always available as the
        # explicit fallback (fail-safe text preservation).
        if self.policy == ChunkPolicy.ADAPTIVE_VI:
            if fixed_config is None:
                fixed_config = FixedChunkPolicyConfig(
                    min_chars=min_chars, target_chars=target_chars, max_chars=max_chars
                )
            if adaptive_config is None:
                # The constructor's char-size knob doubles as the adaptive
                # char-bias reference (stamped as the config's own constant —
                # scoring never receives the fixed config's target_chars).
                adaptive_config = AdaptiveViPolicyConfig(
                    min_chars=min_chars, max_chars=max_chars, char_bias_chars=target_chars
                )
            self._fixed_strategy = FixedPolicyStrategy(fixed_config)
            self._strategy: ChunkingPolicy = AdaptiveViPolicyStrategy(adaptive_config)
        else:
            if fixed_config is None:
                fixed_config = FixedChunkPolicyConfig(
                    min_chars=min_chars, target_chars=target_chars, max_chars=max_chars
                )
            self._fixed_strategy = FixedPolicyStrategy(fixed_config)
            self._strategy = self._fixed_strategy
        # Once adaptive analysis fails for an utterance, the chunker fails
        # closed to fixed segmentation for the rest of the utterance and
        # stamps ``fixed_fallback`` (design "Failure handling").
        self._fallback_active = False
        self._fallback_reason: Optional[str] = None
        self._telemetry = telemetry
        self._buffer: list[str] = []
        self._buffer_len = 0
        self._buffer_started_at: Optional[float] = None
        self._seq = 0

    # -- internal helpers -------------------------------------------------

    def _emit(
        self,
        text: str,
        is_final: bool,
        reason: str | ChunkDecisionReason,
        *,
        protected_fallback: bool = False,
    ) -> TextChunk:
        # StrEnum members serialize as their stable value; plain strings
        # (e.g. a caller-supplied flush reason) pass through unchanged.
        reason_value = reason.value if isinstance(reason, ChunkDecisionReason) else reason
        chunk = TextChunk(
            session_id=self.session_id,
            utterance_id=self.utterance_id,
            seq=self._seq,
            text=text,
            is_final=is_final,
            decision_reason=reason_value,
        )
        # Content-free telemetry at the single emit chokepoint (task 7.1):
        # only lengths, the reason value, flags, and policy id — never text.
        if self._telemetry is not None:
            self._telemetry.record_chunk(
                ChunkTelemetry(
                    seq=self._seq,
                    decision_reason=reason_value,
                    char_length=len(text),
                    estimated_duration_ms=(
                        self._estimator.estimate_ms(text) if self._estimator is not None else None
                    ),
                    hard_max_used=reason_value == ChunkDecisionReason.HARD_MAX,
                    protected_span_fallback=protected_fallback,
                    policy=self.policy.value,
                    is_final=is_final,
                )
            )
        self._seq += 1
        return chunk

    def _reset_buffer(self) -> None:
        self._buffer = []
        self._buffer_len = 0
        self._buffer_started_at = None

    def _start_buffer_clock(self) -> None:
        # Age starts only when the first non-empty fragment enters an empty
        # buffer; long TTFT before any text must never count as buffer age.
        if self._buffer_started_at is None:
            self._buffer_started_at = self._clock()

    # -- strategy plumbing -------------------------------------------------

    @property
    def buffer_len(self) -> int:
        """Length of the uncommitted buffer (public for strategy use)."""
        return self._buffer_len

    def _commit_tail(self, tail: str) -> None:
        """Retain the uncommitted remainder after a committed split."""
        self._buffer = [tail] if tail else []
        self._buffer_len = len(tail)
        self._buffer_started_at = self._clock() if tail else None

    def _enter_fixed_fallback(self, reason: str) -> None:
        """Fail closed to the fixed strategy for the rest of the utterance."""
        self._fallback_active = True
        self._fallback_reason = reason
        self._strategy = self._fixed_strategy

    def _drain(self, runtime_hints: Optional[RuntimeHints]) -> list[TextChunk]:
        return self._strategy.drain(self, runtime_hints)

    # -- public API -------------------------------------------------------

    @property
    def buffered_text(self) -> str:
        """The uncommitted text currently held in the buffer (exact)."""
        return "".join(self._buffer)

    @property
    def buffer_started_at(self) -> Optional[float]:
        """Monotonic time the current buffer received its first text, or None."""
        return self._buffer_started_at

    @property
    def buffer_age_ms(self) -> float:
        """Age of the current buffer in ms; 0.0 while the buffer is empty."""
        if self._buffer_started_at is None:
            return 0.0
        return (self._clock() - self._buffer_started_at) * 1000.0

    @property
    def fallback_active(self) -> bool:
        """True once adaptive analysis failed and fixed fallback took over."""
        return self._fallback_active

    def feed(
        self, token_text: str, runtime_hints: Optional[RuntimeHints] = None
    ) -> list[TextChunk]:
        """Accumulate a fragment and drain any completed phrases.

        Returns zero, one, or many chunks; exact text is never dropped,
        duplicated, or reordered. Runtime hints are a no-op under the fixed
        policy and adjust the adaptive soft duration target (startup/steady/
        starvation) without changing the hard invariants.
        """
        if token_text == "":
            return []
        self._start_buffer_clock()
        self._buffer.append(token_text)
        self._buffer_len += len(token_text)
        return self._drain(runtime_hints)

    def flush(
        self,
        reason: str | ChunkDecisionReason = ChunkDecisionReason.LATENCY_DEADLINE,
        runtime_hints: Optional[RuntimeHints] = None,
    ) -> list[TextChunk]:
        """Force-flush the buffer as a non-final chunk (may be sub-min).

        ``reason`` stamps the chunk's decision reason. A plain string is
        stored exactly; a ``ChunkDecisionReason`` member serializes to its
        stable string value (e.g. ``latency_deadline``).
        """
        del runtime_hints  # explicit caller flush never uses runtime hints
        return self._flush_buffer(is_final=False, reason=reason)

    def finalize(self, runtime_hints: Optional[RuntimeHints] = None) -> list[TextChunk]:
        """Flush the remaining buffer as the final chunk(s) of the utterance.

        An empty buffer returns [] — completion with no textual remainder is
        handled by orchestration finality, not by fabricating an empty
        terminal chunk. The active strategy owns the finalization policy:
        the fixed strategy may split an over-target pending buffer at the
        whitespace nearest ``target_chars`` (a non-final FIXED_FALLBACK head
        plus the exact final FINALIZE remainder); the adaptive strategy
        emits the last coherent phrase as one final chunk (with a defensive
        drain preserving the hard cap).
        """
        return self._strategy.finalize(self, runtime_hints)

    def _flush_buffer(self, is_final: bool, reason: str | ChunkDecisionReason) -> list[TextChunk]:
        if self._buffer_len == 0:
            return []
        chunk = self._emit("".join(self._buffer), is_final=is_final, reason=reason)
        self._reset_buffer()
        return [chunk]
