"""Free-form product text -> facts the merchant ticks (nothing is speakable until the owner approves it)."""

from .extractor import (
    ALLOWED_TYPES,
    DroppedFact,
    ExtractedFact,
    ExtractionResult,
    IntakeError,
    build_prompt,
    extract_facts,
)

__all__ = [
    "ALLOWED_TYPES",
    "DroppedFact",
    "ExtractedFact",
    "ExtractionResult",
    "IntakeError",
    "build_prompt",
    "extract_facts",
]
