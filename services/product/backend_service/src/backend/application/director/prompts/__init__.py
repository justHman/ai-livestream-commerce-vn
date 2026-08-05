"""Fixed Vietnamese Director prompt bundle.

Ownership per OpenSpec 1.11: the four Markdown files in this directory are the
single source of truth for Director business prompts. No Python literal or
env-provided path may replace them. See ``loader.py`` for the fixed-file
loader and ``composer.py`` for decision/fallback composition.
"""

from __future__ import annotations

from .composer import (
    BOUNDARY_BEGIN,
    BOUNDARY_END,
    ContextBundle,
    compose_decision_prompt,
    compose_fallback_prompt,
)
from .loader import (
    ALL_PROMPT_NAMES,
    PromptBundle,
    PromptBundleValidationError,
    PromptName,
    bundle_content_hash,
    bundle_metadata,
    bundle_token_count,
    load_bundle,
)

__all__ = [
    "ALL_PROMPT_NAMES",
    "BOUNDARY_BEGIN",
    "BOUNDARY_END",
    "ContextBundle",
    "PromptBundle",
    "PromptBundleValidationError",
    "PromptName",
    "bundle_content_hash",
    "bundle_metadata",
    "bundle_token_count",
    "compose_decision_prompt",
    "compose_fallback_prompt",
    "load_bundle",
]
