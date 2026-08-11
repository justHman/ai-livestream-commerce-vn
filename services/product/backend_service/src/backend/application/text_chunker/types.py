"""Canonical speech-text chunking types.

``TextChunk`` is the single canonical class for speech-text chunks. All
other types support the deterministic fixed chunk policy and the adaptive
scoring; none carries source identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional
from uuid import uuid4

__all__ = [
    "TextChunk",
    "FixedChunkPolicyConfig",
    "ChunkPolicy",
    "RuntimeHints",
    "ChunkDecisionReason",
]


@dataclass(frozen=True)
class TextChunk:
    """A speech-text fragment for one utterance.

    ``is_final=False`` by default so callers that construct chunks
    positionally keep working; ``decision_reason`` stamps why the chunk was
    committed (populated by ``TextChunker.flush(reason=...)`` and friends).
    """

    session_id: str
    utterance_id: str
    seq: int
    text: str
    is_final: bool = False
    id: str = field(default_factory=lambda: uuid4().hex)
    decision_reason: Optional[str] = None


@dataclass(frozen=True)
class FixedChunkPolicyConfig:
    """Fixed character-threshold policy configuration.

    The deterministic baseline and explicit runtime rollback. ``min_chars``
    is the quality floor for automatic emission, ``target_chars`` the
    finalize fallback boundary, and ``max_chars`` the hard safety cap for
    every automatic non-final chunk (an intentional invariant under BOTH
    policies). Adaptive scoring never consumes ``target_chars`` — the
    adaptive path has its own config with its own tie-break reference.
    """

    min_chars: int = 12
    target_chars: int = 40
    max_chars: int = 80

    def __post_init__(self) -> None:
        if self.min_chars <= 0:
            raise ValueError(f"min_chars must be > 0, got {self.min_chars}")
        if self.target_chars <= 0:
            raise ValueError(f"target_chars must be > 0, got {self.target_chars}")
        if self.max_chars <= 0:
            raise ValueError(f"max_chars must be > 0, got {self.max_chars}")
        if not (self.min_chars <= self.target_chars <= self.max_chars):
            raise ValueError(
                f"require min_chars <= target_chars <= max_chars, got "
                f"min={self.min_chars}, target={self.target_chars}, max={self.max_chars}"
            )


class ChunkPolicy(StrEnum):
    """Deterministic chunking policy (source-agnostic).

    ``FIXED`` is the deterministic character-threshold baseline and explicit
    runtime rollback. ``ADAPTIVE_VI`` selects deterministic Vietnamese
    boundary scoring; the fixed policy remains the default until the VieNeu
    benchmark gate passes.
    """

    FIXED = "fixed"
    ADAPTIVE_VI = "adaptive_vi"


@dataclass(frozen=True)
class RuntimeHints:
    """Source-agnostic runtime facts for soft-target adjustment.

    Never carries source identity (no llm/script/approved). These fields are
    consumed by adaptive scoring only; the fixed policy treats them as
    no-ops.
    """

    speech_start_elapsed_ms: float = 0.0
    playback_buffer_ms: Optional[float] = None
    tts_first_audio_ewma_ms: Optional[float] = None
    tts_rtf_ewma: Optional[float] = None


class ChunkDecisionReason(StrEnum):
    """Why a chunk was committed. String enum so values serialize as text.

    ``punctuation``/``hard_max``/``latency_deadline``/``finalize`` come from
    the fixed policy and orchestration; ``paragraph``/``sentence``/etc. come
    from adaptive boundary scoring; ``fixed_fallback`` stamps every chunk
    after adaptive analysis failed closed to the fixed policy.
    """

    PARAGRAPH = "paragraph"
    SENTENCE = "sentence"
    CLAUSE = "clause"
    PUNCTUATION = "punctuation"
    TARGET = "target"
    LATENCY_DEADLINE = "latency_deadline"
    HARD_MAX = "hard_max"
    FINALIZE = "finalize"
    FIXED_FALLBACK = "fixed_fallback"
