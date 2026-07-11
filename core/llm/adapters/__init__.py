"""core.llm.adapters — lazy-registered inference backends.

Importing this package registers all bundled adapters into
core.llm.base.ENGINES. Each adapter defers its heavy import (vllm, llama_cpp,
transformers, sglang) to from_config() so the base module stays import-safe
even when optional deps are missing.
"""

from __future__ import annotations

# Import order: most-used first. Each module's top-level import is cheap
# (only stdlib + core.llm.base); the model libraries load in from_config().
from . import llamacpp       # noqa: F401
from . import transformers   # noqa: F401
from . import vllm           # noqa: F401
from . import sglang         # noqa: F401
from . import openai_compat  # noqa: F401  remote OpenAI-compat HTTP client

__all__ = ["llamacpp", "transformers", "vllm", "sglang", "openai_compat"]
