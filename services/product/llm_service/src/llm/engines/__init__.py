"""Self-host LLM engines (vllm | sglang | transformers | llamacpp).

llamacpp remains for the legacy offline/Colab GGUF path; the core shim's
``ENGINES`` registry and the parity contract require it.
"""

from . import llamacpp, sglang, transformers, vllm

__all__ = ["llamacpp", "sglang", "transformers", "vllm"]
