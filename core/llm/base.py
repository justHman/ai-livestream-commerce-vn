"""Compatibility import for the canonical LLM engine package."""

from llm.engines.base import *  # noqa: F403
from llm.engines.base import _NoopEngine as _NoopEngine

from .adapters import openai_compat as _openai_compat  # noqa: F401
