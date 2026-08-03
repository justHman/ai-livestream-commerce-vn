"""Canonical self-host LLM service package."""

from llm.bootstrap.app_factory import create_app
from llm.engines.base import (
    ENGINES,
    EngineError,
    EngineUnavailable,
    LLMEngine,
    LLMRequest,
    LLMResponse,
    load_engine,
    register_engine,
    to_llm_fn,
)

__all__ = [
    "ENGINES",
    "EngineError",
    "EngineUnavailable",
    "LLMEngine",
    "LLMRequest",
    "LLMResponse",
    "create_app",
    "load_engine",
    "register_engine",
    "to_llm_fn",
]