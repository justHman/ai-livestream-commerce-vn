"""Canonical streaming text chunker (OpenSpec adaptive-speech-text-chunking).

Source-agnostic segmentation state machine: coalesces arbitrary text
fragments into phrase-sized ``TextChunk`` values under a selectable policy
(``fixed`` or ``adaptive_vi``). The canonical types live in
``backend.application.speech_chunking``; this facade keeps the legacy
import path while forwarding the canonical ``TextChunk`` class.

Behavior contract:
- ``feed()`` appends arbitrary text, scans the accumulated buffer for
  boundaries, and drains ALL completed phrases — one call may return many
  chunks. Every automatic non-final chunk is ``<= max_chars``; oversized
  buffers are split at the cap with remainder retained.
- ``flush(reason=...)`` commits the current buffer as one non-final chunk
  (sub-min allowed, explicit caller action).
- ``finalize()`` emits the remaining buffer as the final chunk; an empty
  buffer returns ``[]`` — completion with no textual remainder is handled
  by orchestration finality (task 6.x).
- ``check_timeout()`` preserves the legacy poll interface for callers/tests
  (task 2.5 keeps ``flush_timeout_ms`` in scope); it measures age from
  ``buffer_started_at``, which starts only when the first non-empty
  fragment enters an empty buffer — long TTFT never ages an empty buffer.

Policies (task 3.7):
- ``fixed``: the historical deterministic rule (first punctuation with a
  ``[min, max]`` prefix, then last whitespace at/before the cap, then the
  exact cap). Behavior is byte-for-byte unchanged.
- ``adaptive_vi``: deterministic Vietnamese boundary selection. During the
  drain loop the chunker commits the earliest strong (paragraph/sentence),
  non-protected boundary at/after ``min_chars`` — a function of the
  accumulated prefix alone, so segmentation is invariant to how the input
  was fragmented. Weak boundaries (clause/comma/cue/whitespace) are never
  committed prematurely; they only decide forced hard-cap splits. The hard
  cap always wins when no natural boundary exists at or before ``max_chars``.
  If adaptive analysis fails (exception or non-finite estimate), the
  utterance fails closed to ``fixed`` segmentation and every subsequent
  chunk is stamped ``fixed_fallback`` — text is never dropped or reordered.

No timers or threads: realtime waiting belongs to orchestration.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .speech_chunking.boundaries import extract_candidates
from .speech_chunking.duration import SpeechDurationEstimator
from .speech_chunking.policy import chunk_decision_reason, select_boundary
from .speech_chunking.types import ChunkDecisionReason, ChunkPolicy, RuntimeHints, TextChunk

__all__ = ["TextChunk", "TextChunker"]


# Phrase boundary characters for the fixed policy's drain loop
# (accepted fixed punctuation: . , ! ? ; : newline).
PUNCTUATION_BOUNDARIES = frozenset({".", ",", "!", "?", ";", ":", "\n"})

# Adaptive analysis horizon: only the first ``max_chars + _ADAPTIVE_HORIZON``
# characters of the buffer are scanned per drain iteration. Every committed
# boundary is at or before ``max_chars``, and protected-span detection for
# candidates at/before the cap only needs spans covering that prefix, so the
# bounded horizon preserves protection flags while keeping a single huge
# delta linear in input size instead of O(n^2) rescanning.
# ponytail: fixed horizon; if a single protected token longer than
# ``max_chars + 256`` must be protected across a forced split, widen it.
_ADAPTIVE_HORIZON = 256


class TextChunker:
    """Coalesce arbitrary text fragments into phrase-sized TextChunks.

    Stateful: holds a text buffer, the monotonic time the current buffer
    first received text (``buffer_started_at``), and a monotonically
    increasing ``seq``. Deterministic fixed segmentation only.
    """

    def __init__(
        self,
        session_id: str,
        utterance_id: str,
        min_chars: int = 12,
        target_chars: int = 40,
        max_chars: int = 80,
        flush_timeout_ms: int = 350,
        clock: Optional[Callable[[], float]] = None,
        policy: str | ChunkPolicy = "fixed",
        estimator: Optional[SpeechDurationEstimator] = None,
    ) -> None:
        if min_chars <= 0:
            raise ValueError(f"min_chars must be > 0, got {min_chars}")
        if target_chars <= 0:
            raise ValueError(f"target_chars must be > 0, got {target_chars}")
        if max_chars <= 0:
            raise ValueError(f"max_chars must be > 0, got {max_chars}")
        if not (min_chars <= target_chars <= max_chars):
            raise ValueError(
                f"require min_chars <= target_chars <= max_chars, got "
                f"min={min_chars}, target={target_chars}, max={max_chars}"
            )
        if flush_timeout_ms < 0:
            raise ValueError(f"flush_timeout_ms must be >= 0, got {flush_timeout_ms}")
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
        self._flush_timeout_s = flush_timeout_ms / 1000.0
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._estimator = (
            estimator
            if estimator is not None
            else SpeechDurationEstimator()
            if self.policy == ChunkPolicy.ADAPTIVE_VI
            else None
        )
        # Once adaptive analysis fails for an utterance, the chunker fails
        # closed to fixed segmentation for the rest of the utterance and
        # stamps ``fixed_fallback`` (design "Failure handling").
        self._fallback_active = False
        self._fallback_reason: Optional[str] = None
        self._buffer: list[str] = []
        self._buffer_len = 0
        self._buffer_started_at: Optional[float] = None
        self._seq = 0

    # -- internal helpers -------------------------------------------------

    def _emit(self, text: str, is_final: bool, reason: str | ChunkDecisionReason) -> TextChunk:
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

    def _drain(self, runtime_hints: Optional[RuntimeHints]) -> list[TextChunk]:
        if self.policy == ChunkPolicy.ADAPTIVE_VI and not self._fallback_active:
            return self._drain_adaptive(runtime_hints)
        return self._drain_until_cap()

    def _drain_until_cap(self) -> list[TextChunk]:
        """Drain completed punctuation phrases and hard-cap splits (fixed).

        The fixed policy commits the FIRST punctuation boundary
        (``. , ! ? ; :`` newline) whose prefix is at least ``min_chars``
        (forward scan over the accumulated buffer — punctuation inside an
        arbitrary delta counts), then keeps cutting at ``max_chars`` so
        every automatic non-final chunk respects the hard cap. Remainders
        are retained exactly.
        """
        chunks: list[TextChunk] = []
        while self._buffer_len > 0:
            start = "".join(self._buffer)
            boundary = self._next_boundary(start)
            if boundary is None:
                break
            end, base_reason = boundary
            head = start[:end]
            tail = start[end:]
            reason = ChunkDecisionReason.FIXED_FALLBACK if self._fallback_active else base_reason
            chunks.append(self._emit(head, is_final=False, reason=reason))
            self._buffer = [tail] if tail else []
            self._buffer_len = len(tail)
            self._buffer_started_at = self._clock() if tail else None
        return chunks

    def _drain_adaptive(self, runtime_hints: Optional[RuntimeHints]) -> list[TextChunk]:
        """Drain under the adaptive policy.

        Per iteration: run the deterministic scorer over the accumulated
        buffer and commit the selected boundary. A selected boundary is
        either the earliest strong (paragraph/sentence), non-protected
        boundary at/after ``min_chars``, or a forced hard-cap split when the
        buffer exceeds ``max_chars`` and no safe strong boundary exists.
        The hard-cap split prefers the best natural (weak) boundary by
        composite score — linguistic quality, then estimated-duration
        proximity, then ``target_chars`` — and falls back to the exact-cap
        cut only when no natural boundary exists.

        Any analysis failure (exception or non-finite estimate) fails closed
        to fixed segmentation: the chunker switches to ``_drain_until_cap``
        for the rest of the utterance, stamps ``fixed_fallback``, and keeps
        every already-emitted chunk — text is never dropped or reordered.
        """
        chunks: list[TextChunk] = []
        while self._buffer_len > 0:
            text = "".join(self._buffer)
            # Bounded analysis horizon: candidates at/before ``max_chars`` only
            # need protected spans covering that prefix, and the earliest strong
            # boundary / forced-cap split are pure functions of the prefix — so
            # scanning past ``max_chars + _ADAPTIVE_HORIZON`` adds nothing but
            # O(n^2) rescanning for a huge single delta.
            prefix = text[: self.max_chars + _ADAPTIVE_HORIZON]
            try:
                candidates = extract_candidates(prefix, self.max_chars)
                selected = select_boundary(
                    text,
                    candidates,
                    estimator=self._estimator,
                    target_chars=self.target_chars,
                    max_chars=self.max_chars,
                    min_chars=self.min_chars,
                    runtime_hints=runtime_hints,
                )
            except Exception as exc:  # noqa: BLE001 — fail closed, never crash
                self._fallback_active = True
                self._fallback_reason = f"adaptive_analysis_error: {type(exc).__name__}"
                return chunks + self._drain_until_cap()
            if selected is None:
                break
            end = selected.candidate.end
            # Hold a strong boundary at the buffer edge that is a "." directly
            # after a digit run: the run may continue into a decimal/grouped
            # number once more text arrives, which would make the dot protected
            # (interior). Committing it early would split a future "199.000đ" —
            # a fragmentation-dependent boundary. The same buffer content
            # always makes the same hold decision, so segmentation stays
            # invariant to how the input was fed.
            if (
                not selected.forced
                and end == len(text)
                and text[end - 1] == "."
                and end >= 2
                and text[end - 2].isdigit()
            ):
                break
            head = text[:end]
            tail = text[end:]
            forced = selected.forced
            reason = (
                ChunkDecisionReason.HARD_MAX
                if forced
                else chunk_decision_reason(selected.candidate.kind)
            )
            chunks.append(self._emit(head, is_final=False, reason=reason))
            self._buffer = [tail] if tail else []
            self._buffer_len = len(tail)
            self._buffer_started_at = self._clock() if tail else None
        return chunks

    def _next_boundary(self, text: str) -> Optional[tuple[int, ChunkDecisionReason]]:
        """First qualifying split in ``text``: punctuation or the hard cap.

        A punctuation boundary qualifies only when its prefix lands in
        ``[min_chars, max_chars]``; without one, the hard cap is reached at
        exactly ``max_chars`` (progress is guaranteed: 1 <= end <= max_chars).
        Returns None while the buffer stays pending.
        """
        if self._buffer_len >= self.min_chars:
            for index, char in enumerate(text):
                if char in PUNCTUATION_BOUNDARIES and self.min_chars <= index + 1 <= self.max_chars:
                    return index + 1, ChunkDecisionReason.PUNCTUATION
        if self._buffer_len >= self.max_chars:
            # Safe fixed-core fallback: no qualifying punctuation was found,
            # so prefer the LAST whitespace at or before the cap — the head
            # then ends on a word boundary and keeps that whitespace, so
            # exact slicing/order stays trivial. Only when the split position
            # is >= min_chars; otherwise cut exactly at the cap. HARD_MAX is
            # stamped either way: the cap forced the decision.
            for split_at in range(self.max_chars, 0, -1):
                if text[split_at - 1].isspace() and split_at >= self.min_chars:
                    return split_at, ChunkDecisionReason.HARD_MAX
            return self.max_chars, ChunkDecisionReason.HARD_MAX
        return None

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
        policy and a soft-target input under the adaptive policy.
        """
        if token_text == "":
            return []
        self._start_buffer_clock()
        self._buffer.append(token_text)
        self._buffer_len += len(token_text)
        chunks = self._drain(runtime_hints)
        # Legacy compatibility: feed() also fires the timeout poll so
        # callers/tests that only call feed() still observe deadline flushes.
        if self._check_timeout():
            chunks.extend(
                self._flush_buffer(is_final=False, reason=ChunkDecisionReason.LATENCY_DEADLINE)
            )
        return chunks

    def check_timeout(self) -> list[TextChunk]:
        """Poll-only flush on timeout (no new text); legacy callers/tests.

        Measured from ``buffer_started_at``; a sub-min buffer never fires.
        """
        if self._check_timeout():
            return self._flush_buffer(is_final=False, reason=ChunkDecisionReason.LATENCY_DEADLINE)
        return []

    def _check_timeout(self) -> bool:
        if self._buffer_started_at is None or self._buffer_len < self.min_chars:
            return False
        return (self._clock() - self._buffer_started_at) >= self._flush_timeout_s

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
        handled by orchestration finality (task 6.x), not by fabricating an
        empty terminal chunk.

        Fixed policy: the pending buffer is necessarily < max_chars (the
        drain loop splits at len >= max_chars), so target_chars gets its real
        role here: when the buffer exceeds target_chars, split ONCE at the
        whitespace nearest target_chars within [min_chars, len-1] (ties prefer
        the lower index). The head is emitted as a non-final FIXED_FALLBACK
        chunk — the target is only a fallback, not a deadline — and the
        remainder follows as the exact final FINALIZE chunk. With no
        qualifying whitespace, or when target_chars >= len, the whole buffer
        is one final chunk.

        Adaptive policy: the pending buffer is the last coherent phrase (the
        drain already committed strong and forced-cap boundaries), so it is
        emitted as one final chunk. A defensive drain first guarantees the
        hard cap if a caller left an oversized buffer.
        """
        if self._buffer_len == 0:
            return []
        if self.policy == ChunkPolicy.ADAPTIVE_VI and not self._fallback_active:
            return self._finalize_adaptive(runtime_hints)
        text = "".join(self._buffer)
        if self._buffer_len > self.target_chars:
            # Target fallback only at finalize of a pending buffer below the
            # cap: no punctuation drained and no hard-max split applies, so
            # the closest whitespace around target_chars fixes the boundary.
            best: Optional[int] = None
            for index in range(self.min_chars, self._buffer_len):
                if not text[index - 1].isspace():
                    continue
                if best is None or abs(index - self.target_chars) < abs(best - self.target_chars):
                    best = index
            if best is not None:
                head = self._emit(
                    text[:best], is_final=False, reason=ChunkDecisionReason.FIXED_FALLBACK
                )
                self._buffer = [text[best:]]
                self._buffer_len = len(text[best:])
                self._buffer_started_at = self._clock() if self._buffer_len else None
                return [head] + self._flush_buffer(
                    is_final=True, reason=ChunkDecisionReason.FINALIZE
                )
        return self._flush_buffer(is_final=True, reason=ChunkDecisionReason.FINALIZE)

    def _finalize_adaptive(self, runtime_hints: Optional[RuntimeHints]) -> list[TextChunk]:
        """Adaptive finalize: the last coherent phrase is one final chunk.

        The drain loop keeps the pending buffer <= ``max_chars`` (forced-cap
        splits), so this path normally emits the whole buffer as the final
        chunk. A defensive drain first preserves the hard cap if a caller
        constructed state with an oversized buffer.
        """
        if self._buffer_len == 0:
            return []
        if self._buffer_len > self.max_chars:
            chunks = self._drain_adaptive(runtime_hints)
            if self._buffer_len == 0:
                return chunks
        return self._flush_buffer(is_final=True, reason=ChunkDecisionReason.FINALIZE)

    def _flush_buffer(self, is_final: bool, reason: str | ChunkDecisionReason) -> list[TextChunk]:
        if self._buffer_len == 0:
            return []
        chunk = self._emit("".join(self._buffer), is_final=is_final, reason=reason)
        self._reset_buffer()
        return [chunk]
