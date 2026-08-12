"""Segment authoring pipeline (Tasks 8.1-8.10).

Exposes the Task 8 contracts:

  ContinuityState          - bounded previous-segment tail + covered IDs
  SegmentGenerationResult  - one segment's display/spoken text + continuity
  ProductSegmentGenerator  - exactly one semantic call per preplanned index
  run_segment_step         - sequential continuity + segment gate ordering
"""

from .continuity import ContinuityState
from .segment_generator import (
    ProductSegmentGenerator,
    SegmentGenerationResult,
    SegmentStepOutcome,
    run_segment_step,
)

__all__ = [
    "ContinuityState",
    "SegmentGenerationResult",
    "ProductSegmentGenerator",
    "SegmentStepOutcome",
    "run_segment_step",
]
