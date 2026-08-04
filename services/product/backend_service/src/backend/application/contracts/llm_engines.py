"""LLM engine type contracts — shared registry with the llm_service.

Until the 1.50-1.59 HTTP-client refactor replaces in-process engine loading,
the backend re-exports the canonical llm_service engine seam so the ENGINES
registry is shared (tests and the EngineManager register/load into the same
registry). core/ stays untouched; llm_service is the canonical engine owner.
"""

from __future__ import annotations

from llm.engines.base import (  # noqa: F401
    ENGINES,
    LLMEngine,
    LLMRequest,
    LLMResponse,
    EngineError,
    EngineUnavailable,
    load_engine,
    register_engine,
    to_llm_fn,
)
from llm.engines.base import _NoopEngine  # noqa: F401
