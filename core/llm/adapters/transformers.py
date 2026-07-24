"""HuggingFace transformers adapter — universal fallback.

Uses `AutoModelForCausalLM` + `AutoTokenizer` — the most compatible backend
(works with any HF model, CPU or GPU). Slower than vLLM/llama.cpp but
zero-surprise: if a model is on HF, this adapter loads it.

Use cases:
  - Development / debugging on a machine without vLLM.
  - Models not yet supported by vLLM (new architectures, custom code).
  - CPU-only fallback (set device="cpu").

Usage:
    llm = load_engine({
        "engine": "hf",
        "model": "Qwen/Qwen3-4B-Instruct",
        "device": "cuda",            # or "cpu", or "auto"
        "dtype": "auto",             # "auto" | "float16" | "bfloat16" | "float32"
        "trust_remote_code": False,
    })
"""

from __future__ import annotations

from typing import Iterator

from ..base import LLMEngine, LLMRequest, LLMResponse, register_engine


@register_engine("hf")
class HFTransformersEngine(LLMEngine):
    """HuggingFace transformers backend (universal fallback)."""

    def __init__(self) -> None:
        self._model = None
        self._tokenizer = None
        self._device = "cpu"

    @classmethod
    def from_config(cls, cfg: dict) -> "HFTransformersEngine":
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        e = cls()
        model_id = cfg.get("model") or cfg.get("weights_path")
        if not model_id:
            raise ValueError("hf adapter needs cfg['model'] (HF id or path)")

        dtype_str = cfg.get("dtype", "auto")
        dtype_map = {
            "auto": torch.float16 if torch.cuda.is_available() else torch.float32,
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
            "float32": torch.float32,
        }
        dtype = dtype_map.get(dtype_str, torch.float16)

        device = cfg.get("device", "auto")
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"

        trust = bool(cfg.get("trust_remote_code", False))

        e._tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=trust)
        e._model = AutoModelForCausalLM.from_pretrained(
            model_id, torch_dtype=dtype, trust_remote_code=trust,
        )
        if device == "cuda" and torch.cuda.is_available():
            e._model = e._model.cuda()
        elif device == "cpu":
            e._model = e._model.cpu()
        e._model.eval()
        e._device = device
        e.name = "hf"
        return e

    def generate(self, req: LLMRequest) -> LLMResponse:
        import torch

        prompt = self._tokenizer.apply_chat_template(
            req.messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self._tokenizer(prompt, return_tensors="pt")
        if self._device == "cuda" and torch.cuda.is_available():
            inputs = {k: v.cuda() for k, v in inputs.items()}

        prompt_len = inputs["input_ids"].shape[1]

        with torch.no_grad():
            out = self._model.generate(
                **inputs,
                max_new_tokens=req.max_tokens,
                temperature=req.temperature,
                top_p=req.top_p,
                do_sample=req.temperature > 0,
                repetition_penalty=req.repetition_penalty,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        gen_ids = out[0][prompt_len:]
        text = self._tokenizer.decode(gen_ids, skip_special_tokens=True).strip()
        return LLMResponse(
            text=text,
            finish_reason="length" if len(gen_ids) >= req.max_tokens else "stop",
            num_prompt_tokens=prompt_len,
            num_generated_tokens=len(gen_ids),
            engine=self.name,
        )

    def stream(self, req: LLMRequest) -> Iterator[str]:
        """Use the TextIteratorStreamer for true token streaming."""
        import threading

        import torch
        from transformers import TextIteratorStreamer

        prompt = self._tokenizer.apply_chat_template(
            req.messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self._tokenizer(prompt, return_tensors="pt")
        if self._device == "cuda" and torch.cuda.is_available():
            inputs = {k: v.cuda() for k, v in inputs.items()}

        streamer = TextIteratorStreamer(
            self._tokenizer, skip_prompt=True, skip_special_tokens=True
        )
        gen_kwargs = dict(
            **inputs,
            max_new_tokens=req.max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
            do_sample=req.temperature > 0,
            repetition_penalty=req.repetition_penalty,
            pad_token_id=self._tokenizer.eos_token_id,
            streamer=streamer,
        )
        thread = threading.Thread(target=self._model.generate, kwargs=gen_kwargs)
        thread.start()
        for text in streamer:
            yield text
        thread.join()

    def unload(self) -> None:
        import gc

        self._model = None
        self._tokenizer = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
