"""core.llm — model-agnostic LLM inference seam.

The analogue of core/tts for language models. Every inference backend
implements ONE interface (LLMEngine); swap by config, not code.

Public API:
  LLMEngine, LLMRequest, LLMResponse  — the ABC + request/response types
  load_engine(cfg)                     — build an engine from config dict
  to_llm_fn(engine, system_prompt)     — adapt to the (text)->str callable
                                          that core.render.cloud.configure expects
  ENGINES                              — name -> class registry

Engines (selected by cfg['engine']):
  "vllm"        — production (continuous batching, prefix cache, FP8 KV)
  "llamacpp"    — Colab T4 demo (GGUF Q4_K_M, low VRAM)
  "sglang"      — optional (RadixAttention prefix cache, structured gen)
  "hf"          — universal fallback (AutoModelForCausalLM, CPU/GPU)
  "none"        — noop echo (offline tests / CI, no deps)

Env-driven config (read by core.config.AppConfig):
  LLM_ENGINE          vllm | llamacpp | sglang | hf | none
  LLM_MODEL           HF model id or path
  LLM_MODEL_PATH      local path (for llamacpp GGUF dir)
  LLM_DEVICE          cuda | cpu | auto
  LLM_MAX_TOKENS      default generation length (128)
  LLM_TEMPERATURE     default sampling temperature (0.7)
  LLM_SYSTEM_PROMPT   persona prompt (prepended to every call)
  LLM_MAX_MODEL_LEN   context window (vllm/sglang, 4096)
  LLM_QUANTIZATION    awq | gptq | fp8 | None (vllm)
  + backend-specific env vars (see each adapter)
"""

from __future__ import annotations

from .base import (
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
