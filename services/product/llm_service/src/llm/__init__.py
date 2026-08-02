"""Canonical self-host LLM service package."""

from .engines.base import (
    ENGINES,
    LLMEngine,
    LLMRequest,
    LLMResponse,
    load_engine,
    register_engine,
    to_llm_fn,
)

__all__ = [
    "LLMEngine",
    "LLMRequest",
    "LLMResponse",
    "load_engine",
    "to_llm_fn",
    "register_engine",
    "ENGINES",
]
