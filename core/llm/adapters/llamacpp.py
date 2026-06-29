"""llama.cpp adapter — Colab / single-GPU demo backend.

llama.cpp (`llama-cpp-python`) is the demo / low-concurrency choice:
  - GGUF quantization (Q4_K_M): 4B model fits in ~3GB VRAM on a free Colab T4.
  - Low first-token latency (TTFT) for 1-2 concurrent users.
  - Single-stream prompt cache (no cross-user KV sharing → NOT for production
    multi-session; vLLM is the production path).

Usage:
    from core.llm import load_engine, to_llm_fn
    llm = load_engine({
        "engine": "llamacpp",
        "model_path": "weights/llm/qwen3-4b-q4_k_m.gguf",
        # or "model": "Qwen/Qwen3-4B-GGUF" to auto-download from HF
        "n_ctx": 4096,
        "n_gpu_layers": -1,   # -1 = all on GPU
    })
    cloud.configure(llm=to_llm_fn(llm, system_prompt=PERSONA))

The adapter auto-finds a .gguf in `model_path` dir if a directory is given,
or downloads from HF if a repo id is passed (see `model` cfg key).
"""

from __future__ import annotations

import glob
import os
from typing import Iterator, Optional

from ..base import LLMEngine, LLMRequest, LLMResponse, register_engine
from ...render.windows import TextChunk


@register_engine("llamacpp")
class LlamaCppEngine(LLMEngine):
    """llama.cpp GGUF backend (Colab T4 demo, low VRAM)."""

    def __init__(self) -> None:
        self._llm = None
        self._system_prompt = None

    @classmethod
    def from_config(cls, cfg: dict) -> "LlamaCppEngine":
        from llama_cpp import Llama

        e = cls()

        # Resolve the GGUF path: explicit path > dir glob > HF repo download.
        model_path = cfg.get("model_path") or cfg.get("weights_path")
        model_repo = cfg.get("model")  # e.g. "Qwen/Qwen3-4B-GGUF"

        if model_path and os.path.isdir(model_path):
            ggufs = sorted(glob.glob(os.path.join(model_path, "*.gguf")))
            if not ggufs:
                raise FileNotFoundError(
                    f"No .gguf in {model_path}. Download a Q4_K_M GGUF first."
                )
            model_path = ggufs[0]
        elif model_repo and not model_path:
            # Auto-download from HF Hub (llama-cpp supports repo id)
            pattern = cfg.get("gguf_pattern", "*Q4_K_M*.gguf")
            model_path = f"{model_repo}/{pattern}"

        if not model_path:
            raise ValueError(
                "llamacpp adapter needs cfg['model_path'] or cfg['model'] (HF repo)"
            )

        e._llm = Llama(
            model_path=model_path,
            n_ctx=int(cfg.get("n_ctx", 4096)),
            n_gpu_layers=int(cfg.get("n_gpu_layers", -1)),
            n_threads=int(cfg.get("n_threads", 0)),  # 0 = auto
            verbose=bool(cfg.get("verbose", False)),
            chat_format=cfg.get("chat_format"),  # auto-detect if None
        )
        e._system_prompt = cfg.get("system_prompt")
        e.name = "llamacpp"
        return e

    def generate(self, req: LLMRequest) -> LLMResponse:
        messages = list(req.messages)
        # Ensure a system prompt if the engine-level default is set and none
        # is present in the request.
        if self._system_prompt and not any(m["role"] == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": self._system_prompt})

        out = self._llm.create_chat_completion(
            messages=messages,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
            stop=req.stop or None,
            seed=req.seed,
            repetition_penalty=req.repetition_penalty,
            stream=False,
        )
        choice = out["choices"][0]
        text = choice["message"]["content"].strip()
        finish = choice.get("finish_reason", "stop")
        usage = out.get("usage", {})
        return LLMResponse(
            text=text,
            finish_reason=finish,
            num_prompt_tokens=usage.get("prompt_tokens", 0),
            num_generated_tokens=usage.get("completion_tokens", 0),
            engine=self.name,
        )

    def stream(self, req: LLMRequest) -> Iterator[str]:
        messages = list(req.messages)
        if self._system_prompt and not any(m["role"] == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": self._system_prompt})

        for chunk in self._llm.create_chat_completion(
            messages=messages,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
            stop=req.stop or None,
            seed=req.seed,
            stream=True,
        ):
            delta = chunk["choices"][0].get("delta", {})
            if "content" in delta and delta["content"]:
                yield delta["content"]

    def stream_chunks(
        self,
        req: LLMRequest,
        *,
        session_id: str = "",
        utterance_id: str = "",
    ) -> Iterator[TextChunk]:
        """Native incremental TextChunk stream from llama.cpp's streaming API.

        Overrides the base default to emit ``TextChunk`` objects directly from
        ``create_chat_completion(stream=True)`` without a double-iteration over
        ``stream()``. Uses a one-ahead buffer: each delta is held until the next
        delta arrives, so the last real delta is emitted with
        ``is_final=True`` (no empty sentinel, no text duplication).

        Empty/None content deltas (e.g. the final ``finish_reason`` terminator)
        are skipped — only non-empty text becomes a ``TextChunk``.
        """
        messages = list(req.messages)
        if self._system_prompt and not any(m["role"] == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": self._system_prompt})

        seq = 0
        buffered: Optional[str] = None
        for chunk in self._llm.create_chat_completion(
            messages=messages,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
            stop=req.stop or None,
            seed=req.seed,
            stream=True,
        ):
            delta = chunk["choices"][0].get("delta", {})
            content = delta.get("content") if isinstance(delta, dict) else None
            if not content:
                continue
            if buffered is not None:
                yield TextChunk(
                    session_id=session_id,
                    utterance_id=utterance_id,
                    seq=seq,
                    text=buffered,
                    is_final=False,
                )
                seq += 1
            buffered = content
        if buffered is not None:
            yield TextChunk(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=seq,
                text=buffered,
                is_final=True,
            )

    def unload(self) -> None:
        self._llm = None
        import gc

        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        gc.collect()
