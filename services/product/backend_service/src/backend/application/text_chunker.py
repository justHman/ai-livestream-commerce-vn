"""Canonical streaming text chunker (OpenSpec adaptive-speech-text-chunking).

Source-agnostic segmentation state machine: coalesces arbitrary text
fragments into phrase-sized ``TextChunk`` values using a deterministic
fixed character policy. The canonical types live in
``backend.application.speech_chunking``; this facade keeps the legacy
import path while forwarding the canonical ``TextChunk`` class.

Behavior contract:
- ``feed()`` appends arbitrary text, scans the accumulated buffer for
  punctuation boundaries, and drains ALL completed phrases — one call may
  return many chunks. Every automatic non-final chunk is ``<= max_chars``;
  oversized buffers are split at the cap with remainder retained.
- ``flush(reason=...)`` commits the current buffer as one non-final chunk
  (sub-min allowed, explicit caller action).
- ``finalize()`` emits the remaining buffer as the final chunk; an empty
  buffer returns ``[]`` — completion with no textual remainder is handled
  by orchestration finality (task 6.x).
- ``check_timeout()`` preserves the legacy poll interface for callers/tests
  (task 2.5 keeps ``flush_timeout_ms`` in scope); it measures age from
  ``buffer_started_at``, which starts only when the first non-empty
  fragment enters an empty buffer — long TTFT never ages an empty buffer.

No timers or threads: realtime waiting belongs to orchestration.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .speech_chunking.types import ChunkDecisionReason, ChunkPolicy, RuntimeHints, TextChunk

__all__ = ["TextChunk", "TextChunker"]


# Phrase boundary characters for the fixed policy's drain loop
# (accepted fixed punctuation: . , ! ? ; : newline).
PUNCTUATION_BOUNDARIES = frozenset({".", ",", "!", "?", ";", ":", "\n"})


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
        if policy == ChunkPolicy.ADAPTIVE_VI:
            raise ValueError(
                f"policy {policy!r} is declared but not implemented; "
                "adaptive scoring lands in task 3.7"
            )
        if policy != ChunkPolicy.FIXED:
            raise ValueError(f"unknown policy {policy!r}")
        self.session_id = session_id
        self.utterance_id = utterance_id
        self.min_chars = min_chars
        self.target_chars = target_chars
        self.max_chars = max_chars
        self.policy = ChunkPolicy.FIXED
        self._flush_timeout_s = flush_timeout_ms / 1000.0
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
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

    def _drain_until_cap(self) -> list[TextChunk]:
        """Drain completed punctuation phrases and hard-cap splits.

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
            end, reason = boundary
            head = start[:end]
            tail = start[end:]
            chunks.append(self._emit(head, is_final=False, reason=reason))
            self._buffer = [tail] if tail else []
            self._buffer_len = len(tail)
            self._buffer_started_at = self._clock() if tail else None
        return chunks

    def _next_boundary(self, text: str) -> Optional[tuple[int, ChunkDecisionReason]]:
        """First qualifying split in ``text``: punctuation or the fallbacks.

        A punctuation boundary qualifies only when its prefix lands in
        ``[min_chars, max_chars]``. Without one, once the buffer has reached
        the ``max_chars`` decision horizon the deterministic target fallback
        may split at the whitespace nearest ``target_chars``; if that cannot
        apply (no qualifying whitespace, or a whole-text buffer where
        ``target_chars >= len``), the hard cap is reached at exactly
        ``max_chars``. Progress is guaranteed: 1 <= end <= max_chars.
        Returns None while the buffer stays pending.
        """
        if self._buffer_len >= self.min_chars:
            for index, char in enumerate(text):
                if char in PUNCTUATION_BOUNDARIES and self.min_chars <= index + 1 <= self.max_chars:
                    return index + 1, ChunkDecisionReason.PUNCTUATION
        if self._buffer_len >= self.max_chars and self._buffer_len > self.target_chars:
            # Deterministic target-character fallback (spec "Target character
            # fallback"): once the buffer has reached the max_chars decision
            # horizon, split at the whitespace nearest the target — the head
            # then ends on a word boundary and keeps that whitespace, so
            # exact slicing/order stays trivial. Only when the split position
            # is >= min_chars; without qualifying whitespace the hard-cap
            # fallback below stays authoritative. A whole-text buffer
            # (target_chars >= len) never splits here: cutting would produce
            # a zero-length remainder.
            for split_at in range(self.target_chars, 0, -1):
                if text[split_at - 1].isspace() and split_at >= self.min_chars:
                    return split_at, ChunkDecisionReason.FIXED_FALLBACK
        if self._buffer_len >= self.max_chars and self._buffer_len > self.target_chars:
            # Safe hard-cap fallback: no qualifying punctuation (and no
            # target-fallback whitespace) was found, so prefer the LAST
            # whitespace at or before the cap — the head then ends on a word
            # boundary and keeps that whitespace. Only when the split
            # position is >= min_chars; otherwise cut exactly at the cap.
            # HARD_MAX is stamped either way: the cap forced the decision.
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

    def feed(
        self, token_text: str, runtime_hints: Optional[RuntimeHints] = None
    ) -> list[TextChunk]:
        """Accumulate a fragment and drain any completed phrases.

        Returns zero, one, or many chunks; exact text is never dropped,
        duplicated, or reordered. Runtime hints are a no-op under the fixed
        policy.
        """
        del runtime_hints  # no-op under fixed policy
        if token_text == "":
            return []
        self._start_buffer_clock()
        self._buffer.append(token_text)
        self._buffer_len += len(token_text)
        chunks = self._drain_until_cap()
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
        del runtime_hints  # no-op under fixed policy
        return self._flush_buffer(is_final=False, reason=reason)

    def finalize(self, runtime_hints: Optional[RuntimeHints] = None) -> list[TextChunk]:
        """Flush the remaining buffer as the final chunk of the utterance.

        An empty buffer returns [] — completion with no textual remainder is
        handled by orchestration finality (task 6.x), not by fabricating an
        empty terminal chunk.
        """
        del runtime_hints  # no-op under fixed policy
        if self._buffer_len == 0:
            return []
        text = "".join(self._buffer)
        if self._buffer_len >= self.max_chars and self._buffer_len > self.target_chars:
            # Deterministic target-character fallback at finalize: split the
            # final no-punctuation pending text at the whitespace nearest the
            # target, keeping the nonfinal head >= min_chars and preserving
            # the remaining text exactly. A whole-text buffer (target >= len)
            # stays one final chunk.
            for split_at in range(self.target_chars, 0, -1):
                if text[split_at - 1].isspace() and split_at >= self.min_chars:
                    return [
                        self._emit(
                            text[:split_at],
                            is_final=False,
                            reason=ChunkDecisionReason.FIXED_FALLBACK,
                        ),
                        self._emit(
                            text[split_at:], is_final=True, reason=ChunkDecisionReason.FINALIZE
                        ),
                    ]
        return self._flush_buffer(is_final=True, reason=ChunkDecisionReason.FINALIZE)

    def _flush_buffer(self, is_final: bool, reason: str | ChunkDecisionReason) -> list[TextChunk]:
        if self._buffer_len == 0:
            return []
        chunk = self._emit("".join(self._buffer), is_final=is_final, reason=reason)
        self._reset_buffer()
        return [chunk]
