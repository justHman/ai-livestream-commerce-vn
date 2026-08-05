"""LLMEngine — model-agnostic LLM inference seam.

The analogue of tts/engines/base.py for language models. Every inference backend
(vLLM production, llama.cpp Colab demo, SGLang RadixAttention, HF transformers
fallback) implements ONE interface. The Director/RenderBackend never depends on
a specific engine; swap by changing config, not code.

Design (mirrors the TTS seam):
  LLMEngine ABC          — from_config(cfg) + generate(req) + stream(req)
  LLMRequest             — messages + sampling params (engine-agnostic)
  LLMResponse            — text + finish_reason + token counts
  ENGINES registry       — name -> class; adapters self-register on import
  load_engine(cfg)       — build an engine from a config dict
  to_llm_fn(engine, ...) — adapt to the (text)->str callable that
                           the avatar render seam expects

Why a unified seam (not per-model native libs):
  - vLLM / SGLang / llama.cpp / transformers each have their own API surface.
    Without this seam, every call site couples to one library.
  - Production path (vLLM: continuous batching, prefix cache, FP8 KV) and the
    Colab demo path (llama.cpp: low-VRAM GGUF on T4) share the SAME interface.
    Lift Colab -> AWS by changing env vars, zero code change above this seam.
  - Adding a new backend (e.g. TGI, TensorRT-LLM) = one new adapter file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional
from uuid import uuid4

try:
    from avatar.engines.windows import TextChunk
except ImportError:

    @dataclass(frozen=True)
    class TextChunk:
        """Service-local streamed text chunk."""

        session_id: str
        utterance_id: str
        seq: int
        text: str
        is_final: bool
        id: str = field(default_factory=lambda: uuid4().hex)


class EngineError(RuntimeError):
    """Typed engine failure surfaced at the API boundary."""


class EngineUnavailable(EngineError):
    """Raised when no engine is started or the engine is not ready."""


@dataclass
class LLMRequest:
    """One generation request (engine-agnostic).

    `messages` follows the OpenAI chat format:
      [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
    Engines that only accept raw prompts apply the tokenizer's chat template
    internally (see adapters).

    Sampling fields mirror vLLM/SGLang/llama.cpp common knobs so the same
    request works across all backends. Engines ignore fields they don't support.
    """

    messages: list[dict]
    max_tokens: int = 512
    temperature: float = 0.7
    top_p: float = 0.95
    top_k: int = -1  # -1 = disabled (vLLM convention)
    stop: list[str] = field(default_factory=list)
    seed: int = 42
    repetition_penalty: float = 1.0
    frequency_penalty: float = 0.0
    # Optional JSON Schema for Outlines / guided decoding (openai_compat only).
    response_schema: Optional[dict] = None

    @classmethod
    def from_prompt(
        cls,
        user_text: str,
        system_prompt: Optional[str] = None,
        **kwargs,
    ) -> "LLMRequest":
        """Convenience: build from a single user prompt + optional system."""
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": user_text})
        return cls(messages=msgs, **kwargs)


@dataclass
class LLMResponse:
    """Result of one generation."""

    text: str
    finish_reason: str = "stop"  # "stop" | "length" | "content_filter"
    num_prompt_tokens: int = 0
    num_generated_tokens: int = 0
    engine: str = ""


class LLMEngine(ABC):
    """Model-agnostic LLM interface. One adapter per inference backend.

    Lifecycle:
      from_config(cfg)  — build (load weights, set device, configure runtime)
      generate(req)     — blocking full generation
      stream(req)       — yield text deltas (override for true streaming)
      unload()          — free VRAM
    """

    name: str = "abstract"

    @classmethod
    @abstractmethod
    def from_config(cls, cfg: dict) -> "LLMEngine":
        """Build from cfg = {model, device, dtype, quantization, ...}."""
        ...

    @abstractmethod
    def generate(self, req: LLMRequest) -> LLMResponse:
        """Blocking generation. Returns the full response."""
        ...

    def stream(self, req: LLMRequest) -> Iterator[str]:
        """Default: yield the full text at once. Override for token streaming."""
        yield self.generate(req).text

    def stream_chunks(
        self,
        req: LLMRequest,
        *,
        session_id: str = "",
        utterance_id: str = "",
    ) -> Iterator[TextChunk]:
        """Yield the generation as a stream of :class:`TextChunk` objects.

        This is the streaming-pipeline seam (Task 2): the LLM stage emits
        ``TextChunk`` objects (not bare ``str``) so the downstream text chunker
        and TTS stages can track session/utterance identity and finalization.

        Default implementation: wrap :meth:`stream` (the existing str-delta
        stream) using a one-ahead buffer so the LAST delta's ``TextChunk`` is
        emitted with ``is_final=True`` (without needing to retroactively edit a
        frozen yielded object, and without a spurious empty sentinel). One
        ``TextChunk`` is emitted per str delta with an incrementing ``seq``
        (0-based). If the underlying stream is empty, NO chunks are emitted.

        Adapters with a true incremental generate API (e.g. llama.cpp) MAY
        override this to emit ``TextChunk`` objects directly from the native
        stream — see ``llm.engines.llamacpp.LlamaCppEngine.stream_chunks``.
        The default here is correct for any engine whose ``stream()`` yields
        str deltas; the override exists to avoid a double-iteration in adapters
        that already have a native incremental path.

        Args:
            req: The generation request.
            session_id: Render session identifier propagated to each TextChunk.
            utterance_id: Utterance identifier propagated to each TextChunk.

        Yields:
            TextChunk objects, one per str delta from :meth:`stream`, with the
            last one marked ``is_final=True``.
        """
        seq = 0
        buffered: Optional[str] = None
        for delta in self.stream(req):
            if buffered is not None:
                yield TextChunk(
                    session_id=session_id,
                    utterance_id=utterance_id,
                    seq=seq,
                    text=buffered,
                    is_final=False,
                )
                seq += 1
            buffered = delta
        if buffered is not None:
            yield TextChunk(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=seq,
                text=buffered,
                is_final=True,
            )

    def chat(self, messages: list[dict], **kwargs) -> str:
        """Convenience: generate and return just the text."""
        return self.generate(LLMRequest(messages=messages, **kwargs)).text

    def warmup(self, system_prompt: Optional[str] = None) -> None:
        """Pre-warm the engine: gen one dummy turn to JIT CUDA kernels + populate
        prefix cache with the persona system prompt. Call once after from_config.

        Effect:
        - First real user turn skips CUDA kernel JIT (~1-2s saved on first call).
        - Prefix cache populated → all subsequent turns with the same system
          prompt get a cache hit (0ms prefill for the persona portion).

        Override if an engine needs a different warmup sequence.
        """
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": "hello"})
        try:
            self.generate(LLMRequest(messages=msgs, max_tokens=8, temperature=0.1))
        except Exception:
            pass  # warmup failure is non-fatal

    def unload(self) -> None:
        """Free VRAM when swapping engines (override if needed)."""
        return None


# ── Registry ──────────────────────────────────────────────────────────

ENGINES: dict[str, type[LLMEngine]] = {}


def register_engine(name: str):
    """Class decorator: register an LLMEngine subclass under `name`."""

    def deco(cls: type[LLMEngine]) -> type[LLMEngine]:
        ENGINES[name] = cls
        cls.name = name
        return cls

    return deco


def load_engine(cfg: dict) -> LLMEngine:
    """Build an engine from config. cfg['engine'] selects the adapter.

    Importing llm.engines registers the real adapters lazily; if a
    backend's deps are missing, that adapter raises on from_config (not at
    import), so the base module stays import-safe everywhere.
    """
    engine = cfg.get("engine", "none")
    if engine in ("none", "", None):
        return _NoopEngine()
    if engine not in ENGINES:
        try:
            from . import llamacpp, sglang, transformers, vllm  # noqa: F401
        except Exception:
            pass
    if engine not in ENGINES:
        raise KeyError(f"unknown LLM engine '{engine}'. Registered: {sorted(ENGINES)}")
    return ENGINES[engine].from_config(cfg)


def to_llm_fn(
    engine: LLMEngine,
    system_prompt: Optional[str] = None,
    **default_kwargs,
) -> Callable[[str], str]:
    """Adapt an LLMEngine to the (text)->str callable that
    the avatar render seam expects (llm_fn=...).

    `system_prompt` is prepended to every call (the livestream persona).
    `default_kwargs` override LLMRequest defaults (max_tokens, temperature...).
    """

    def llm_fn(user_text: str) -> str:
        req = LLMRequest.from_prompt(user_text, system_prompt=system_prompt, **default_kwargs)
        return engine.generate(req).text

    return llm_fn


# ── Built-in noop engine (offline / fallback, no deps) ─────────────────


class _NoopEngine(LLMEngine):
    """Deterministic echo fallback — no model, no deps. For offline tests / CI
    so the server runs anywhere. Production replaces this via load_engine()."""

    name = "none"

    @classmethod
    def from_config(cls, cfg: dict) -> "_NoopEngine":
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:
        user = req.messages[-1]["content"] if req.messages else ""
        return LLMResponse(
            text=f"[noop] {user}",
            finish_reason="stop",
            num_prompt_tokens=len(user.split()),
            num_generated_tokens=4,
            engine=self.name,
        )
