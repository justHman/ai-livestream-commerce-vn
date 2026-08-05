"""SGLang adapter — optional high-throughput backend (RadixAttention).

SGLang provides RadixAttention-based prefix caching — even more aggressive
than vLLM's prefix cache for workloads with many shared prefixes (our persona +
product catalog system prompt is identical across sessions → ideal).

Use SGLang when:
  - You need the fastest prefix-cached inference (SGLang's RadixAttention
    can be 5x faster than vLLM for prefix-heavy workloads).
  - You need structured generation (JSON/regex constrained output) via SGLang's
    `RegexConstraint` / `json_decode` — useful for forcing the host to emit
    product-structured responses.

Otherwise vLLM is the default production path. This adapter is opt-in.

Usage:
    llm = load_engine({
        "engine": "sglang",
        "model": "Qwen/Qwen3-4B-Instruct",
        "device": "cuda",
        "mem_fraction_static": 0.9,
        "enable_radix_cache": True,
    })
"""

from __future__ import annotations

from typing import Iterator

from .base import LLMEngine, LLMRequest, LLMResponse, register_engine


@register_engine("sglang")
class SGLangEngine(LLMEngine):
    """SGLang backend (RadixAttention prefix cache + structured generation)."""

    def __init__(self) -> None:
        self._engine = None
        self._tokenizer = None

    @classmethod
    def from_config(cls, cfg: dict) -> "SGLangEngine":
        import sglang as sgl

        e = cls()
        model = cfg.get("model") or cfg.get("weights_path")
        if not model:
            raise ValueError("sglang adapter needs cfg['model']")

        e._engine = sgl.Engine(
            model_path=model,
            mem_fraction_static=float(cfg.get("mem_fraction_static", 0.9)),
            enable_radix_cache=bool(cfg.get("enable_radix_cache", True)),
            trust_remote_code=bool(cfg.get("trust_remote_code", False)),
            context_length=int(cfg.get("max_model_len", 4096)),
        )
        e.name = "sglang"
        return e

    def generate(self, req: LLMRequest) -> LLMResponse:
        prompts = [{"role": m["role"], "content": m["content"]} for m in req.messages]
        out = self._engine.generate(
            prompts,
            sampling_params={
                "max_new_tokens": req.max_tokens,
                "temperature": req.temperature,
                "top_p": req.top_p,
                "top_k": req.top_k if req.top_k > 0 else -1,
                "stop": req.stop or None,
                "repetition_penalty": req.repetition_penalty,
            },
        )
        if isinstance(out, list):
            out = out[0]
        text = out.get("text", "").strip()
        meta = out.get("meta_info", {})
        return LLMResponse(
            text=text,
            finish_reason=meta.get("finish_reason", "stop"),
            num_prompt_tokens=meta.get("prompt_tokens", 0),
            num_generated_tokens=meta.get("completion_tokens", 0),
            engine=self.name,
        )

    def stream(self, req: LLMRequest) -> Iterator[str]:
        prompts = [{"role": m["role"], "content": m["content"]} for m in req.messages]
        for chunk in self._engine.generate(
            prompts,
            sampling_params={
                "max_new_tokens": req.max_tokens,
                "temperature": req.temperature,
                "top_p": req.top_p,
                "stream": True,
            },
        ):
            if isinstance(chunk, dict) and "text" in chunk:
                yield chunk["text"]

    def unload(self) -> None:
        if self._engine is not None:
            self._engine.shutdown()
            self._engine = None
