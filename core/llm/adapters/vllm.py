"""vLLM adapter — PRODUCTION inference backend.

vLLM is the production choice for multi-session live-commerce:
  - Continuous batching: interleaves many sessions' tokens in one GPU batch
    (no per-turn head-of-line blocking — critical when 50+ viewers chat at once).
  - Prefix caching: the persona + product catalog system prompt is fixed and
    long → cache its KV once, reuse across every user/turn. This is the main
    reason production uses vLLM over llama.cpp (llama.cpp prompt cache is
    single-stream, no cross-user KV sharing).
  - PagedAttention: packs KV in pages → more concurrent sessions per GPU.
  - FP8 KV cache (`kv_cache_dtype=fp8`): fits longer context / more sessions.
  - Quantization: awq / gptq / fp8 for 4B-7B models on a single GPU.

Usage:
    from core.llm import load_engine, to_llm_fn
    llm = load_engine({
        "engine": "vllm",
        "model": "Qwen/Qwen3-4B-Instruct",
        "device": "cuda",
        "max_model_len": 4096,
        "quantization": None,        # "awq" | "gptq" | "fp8" | None
        "enable_prefix_caching": True,
        "gpu_memory_utilization": 0.9,
        "dtype": "auto",
    })
    cloud.configure(llm=to_llm_fn(llm, system_prompt=PERSONA))

Swap model = change "model" string. Zero code change above this adapter.
"""

from __future__ import annotations

from typing import Iterator

from ..base import LLMEngine, LLMRequest, LLMResponse, register_engine


@register_engine("vllm")
class VLLMEngine(LLMEngine):
    """vLLM-backed LLM (production: continuous batching + prefix cache)."""

    def __init__(self) -> None:
        self._llm = None
        self._tokenizer = None
        self._default_sampling = None

    @classmethod
    def from_config(cls, cfg: dict) -> "VLLMEngine":
        from vllm import LLM, SamplingParams
        from vllm.transformers_utils.tokenizer import get_tokenizer

        e = cls()
        model = cfg.get("model") or cfg.get("weights_path")
        if not model:
            raise ValueError("vllm adapter needs cfg['model'] (HF id or path)")

        e._llm = LLM(
            model=model,
            dtype=cfg.get("dtype", "auto"),
            quantization=cfg.get("quantization"),
            max_model_len=int(cfg.get("max_model_len", 4096)),
            gpu_memory_utilization=float(cfg.get("gpu_memory_utilization", 0.9)),
            enable_prefix_caching=bool(cfg.get("enable_prefix_caching", True)),
            kv_cache_dtype=cfg.get("kv_cache_dtype", "auto"),
            trust_remote_code=bool(cfg.get("trust_remote_code", False)),
            max_num_seqs=int(cfg.get("max_num_seqs", 64)),
            seed=int(cfg.get("seed", 42)),
            enforce_eager=bool(cfg.get("enforce_eager", False)),
            # Chunked prefill: interleave prefill chunks with decode → lower TTFT
            # for long prompts (persona + catalog system prompt).
            enable_chunked_prefill=bool(cfg.get("enable_chunked_prefill", True)),
            # Speculative decoding: draft model proposes, main model verifies.
            # Only if speculative_model is set (needs a small draft model).
            speculative_model=cfg.get("speculative_model") or None,
            num_speculative_tokens=int(cfg.get("num_speculative_tokens", 5)),
        )
        e._tokenizer = get_tokenizer(
            model, trust_remote_code=cfg.get("trust_remote_code", False)
        )
        e._default_sampling = SamplingParams
        e.name = "vllm"
        return e

    def _to_sampling(self, req: LLMRequest):
        S = self._default_sampling
        return S(
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
            top_k=req.top_k if req.top_k > 0 else -1,
            stop=req.stop or None,
            seed=req.seed,
            repetition_penalty=req.repetition_penalty,
            frequency_penalty=req.frequency_penalty,
        )

    def _apply_chat_template(self, req: LLMRequest) -> str:
        """Convert messages → model-specific chat-formatted prompt."""
        if self._tokenizer is None:
            # fallback: simple concatenation
            return "\n".join(m["content"] for m in req.messages)
        return self._tokenizer.apply_chat_template(
            req.messages, tokenize=False, add_generation_prompt=True
        )

    def generate(self, req: LLMRequest) -> LLMResponse:
        prompt_text = self._apply_chat_template(req)
        sp = self._to_sampling(req)
        outputs = self._llm.generate([prompt_text], sp, use_tqdm=False)
        if not outputs:
            return LLMResponse(text="", engine=self.name)
        out = outputs[0]
        text = out.outputs[0].text.strip()
        finish = out.outputs[0].finish_reason or "stop"
        prompt_toks = len(out.prompt_token_ids) if out.prompt_token_ids else 0
        gen_toks = len(out.outputs[0].token_ids) if out.outputs[0].token_ids else 0
        return LLMResponse(
            text=text,
            finish_reason=finish,
            num_prompt_tokens=prompt_toks,
            num_generated_tokens=gen_toks,
            engine=self.name,
        )

    def stream(self, req: LLMRequest) -> Iterator[str]:
        """vLLM supports streaming via the async engine; for the sync API we
        yield the full text. For true token streaming, use vLLM's AsyncLLMEngine
        directly (future: an async adapter)."""
        yield self.generate(req).text

    def unload(self) -> None:
        import gc
        import torch

        self._llm = None
        self._tokenizer = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
