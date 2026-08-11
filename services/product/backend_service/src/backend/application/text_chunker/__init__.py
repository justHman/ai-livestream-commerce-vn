"""Source-agnostic speech text chunking (OpenSpec adaptive-speech-text-chunking).

One cohesive package: the ``TextChunker`` state machine plus the boundary,
duration, policy, and telemetry modules it composes. Consumers import the
public API from the package root:

    from backend.application.text_chunker import TextChunker, TextChunk

The contract is source-agnostic speech text segmentation: inputs are
arbitrary text fragments (single characters, LLM deltas, sentences,
paragraphs, or an entire script); realtime waiting/deadlines belong to
streaming orchestration, never inside the chunker.
"""

from .boundaries import (
    BoundaryCandidate,
    CandidateKind,
    extract_candidates,
    protected_spans,
)
from .chunker import TextChunker
from .duration import DurationCoefficients, SpeechDurationEstimator
from .policy import (
    AdaptiveAnalysisError,
    SelectedBoundary,
    chunk_decision_reason,
    score_boundary,
    select_boundary,
    soft_target_duration_ms,
)
from .telemetry import BoundedEwma, ChunkTelemetry, TelemetryCollector
from .types import ChunkDecisionReason, ChunkPolicy, RuntimeHints, TextChunk

__all__ = [
    "TextChunker",
    "TextChunk",
    "ChunkPolicy",
    "RuntimeHints",
    "ChunkDecisionReason",
    "SpeechDurationEstimator",
    "DurationCoefficients",
    "BoundaryCandidate",
    "CandidateKind",
    "extract_candidates",
    "protected_spans",
    "SelectedBoundary",
    "select_boundary",
    "score_boundary",
    "chunk_decision_reason",
    "soft_target_duration_ms",
    "AdaptiveAnalysisError",
    "ChunkTelemetry",
    "BoundedEwma",
    "TelemetryCollector",
]
