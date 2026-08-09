"""Canonical speech-text chunking types.

``TextChunk`` is the single canonical class for speech-text chunks; legacy
import paths re-export this class object during migration. All other types
support the deterministic fixed chunk policy (task 2.2) and later adaptive
scoring; none carries source identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional
from uuid import uuid4

__all__ = ["TextChunk", "ChunkPolicy", "RuntimeHints", "ChunkDecisionReason"]


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


class ChunkPolicy(StrEnum):
    """Deterministic chunking policy (source-agnostic).

    Only ``FIXED`` is implemented; adaptive scoring (tasks 3.x) is not
    advertised until the engine exists.
    """

    FIXED = "fixed"


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
    """Why a chunk was committed. String enum so values serialize as text."""

    PUNCTUATION = "punctuation"
    HARD_MAX = "hard_max"
    LATENCY_DEADLINE = "latency_deadline"
    FINALIZE = "finalize"
