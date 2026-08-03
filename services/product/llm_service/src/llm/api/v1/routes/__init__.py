"""Versioned v1 routes for the LLM service."""

from llm.api.v1.routes.chat_completions import router as chat_completions
from llm.api.v1.routes.models import router as models

__all__ = ["chat_completions", "models"]