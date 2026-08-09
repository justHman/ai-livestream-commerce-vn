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
- ``finalize()`` emits the remaining buffer as the final chunk, or stamps
  the last already-emitted chunk final when the buffer is empty.
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


# Phrase boundary characters (punctuation + newline).
_PUNCT_BOUNDARY = frozenset({".", ",", "!", "?", ";", ":", "\n"})
# End-of-chunk punctuation for the fixed policy's drain loop.
_END_PUNCT = frozenset({".", "!", "?", "\n"})


class TextChunker:
    """Coalesce arbitrary text fragments into phrase-sized TextChunks.

    Stateful: holds a text buffer, the monotonic time the current buffer
    first received text (``buffer_started_at``), and a monotonically
    increasing ``seq``. Deterministic fixed segmentation; adaptive scoring
    lands behind the ``adaptive_vi`` policy in later tasks.
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
        if not (min_chars <= target_chars <= max_chars):
            raise ValueError(
                f"require min_chars <= target_chars <= max_chars, got "
                f"min={min_chars}, target={target_chars}, max={max_chars}"
            )
        if flush_timeout_ms < 0:
            raise ValueError(f"flush_timeout_ms must be >= 0, got {flush_timeout_ms}")
        if isinstance(policy, ChunkPolicy):
            policy = policy.name
        if policy not in ("fixed", "adaptive_vi"):
            raise ValueError(f"unknown policy {policy!r}")
        self.session_id = session_id
        self.utterance_id = utterance_id
        self.min_chars = min_chars
        self.target_chars = target_chars
        self.max_chars = max_chars
        self.policy = policy
        self._flush_timeout_s = flush_timeout_ms / 1000.0
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._buffer: list[str] = []
        self._buffer_len = 0
        self._buffer_started_at: Optional[float] = None
        self._seq = 0

    # -- internal helpers -------------------------------------------------

    def _emit(self, text: str, is_final: bool, reason: str) -> TextChunk:
        chunk = TextChunk(
            session_id=self.session_id,
            utterance_id=self.utterance_id,
            seq=self._seq,
            text=text,
            is_final=is_final,
            decision_reason=reason,
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

        The fixed policy commits the FIRST sentence-punctuation boundary
        whose prefix is at least ``min_chars`` (forward scan over the
        accumulated buffer — punctuation inside an arbitrary delta counts),
        then keeps cutting at ``max_chars`` so every automatic non-final
        chunk respects the hard cap. Remainders are retained exactly.
        """
        chunks: list[TextChunk] = []
        while self._buffer_len > 0:
            start = "".join(self._buffer)
            end = -1
            if self._buffer_len >= self.min_chars:
                for index, char in enumerate(start):
                    if char in _END_PUNCT and self.min_chars <= index + 1 <= self.max_chars:
                        end = index + 1
                        break
            if end < 0:
                # No qualifying sentence boundary: cut at the hard cap if
                # reached; otherwise the buffer stays pending.
                if self._buffer_len >= self.max_chars:
                    end = self.max_chars
                else:
                    break
            head = start[:end]
            tail = start[end:]
            chunks.append(self._emit(head, is_final=False, reason=ChunkDecisionReason.PUNCTUATION))
            self._buffer = [tail] if tail else []
            self._buffer_len = len(tail)
            self._buffer_started_at = self._clock() if tail else None
        return chunks

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
        policy (they drive adaptive scoring only).
        """
        del runtime_hints  # no-op under fixed policy; adaptive scoring reads it
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

        ``reason`` stamps the chunk's decision reason; any string or
        ``ChunkDecisionReason`` is accepted.
        """
        del runtime_hints  # no-op under fixed policy; adaptive scoring reads it
        return self._flush_buffer(is_final=False, reason=str(reason))

    def finalize(self, runtime_hints: Optional[RuntimeHints] = None) -> list[TextChunk]:
        """Flush the remaining buffer as the final chunk of the utterance.

        An empty buffer returns [] — completion with no textual remainder is
        handled by orchestration finality (task 6.x), not by fabricating an
        empty terminal chunk.
        """
        del runtime_hints  # no-op under fixed policy; adaptive scoring reads it
        if self._buffer_len > 0:
            return self._flush_buffer(is_final=True, reason=ChunkDecisionReason.FINALIZE)
        return []

    def _flush_buffer(self, is_final: bool, reason: str) -> list[TextChunk]:
        if self._buffer_len == 0:
            return []
        chunk = self._emit("".join(self._buffer), is_final=is_final, reason=reason)
        self._reset_buffer()
        return [chunk]
