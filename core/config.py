"""Portable runtime config — env-driven, same code Colab -> AWS.

12-factor: every deployment-specific value comes from an environment variable.
Colab sets them inline; AWS sets them via task definition / secrets manager.

Engine selection (the production seam):
  RENDER_BACKEND   cloud | self_host(future)
  SESSION_STORE    memory(Colab) | redis(AWS)
  LLM_ENGINE       vllm | llamacpp | sglang | hf | none(default echo)
  TTS_ENGINE       transformers(default) | vieneu | cosyvoice | tone
  DIRECTOR_ENABLED 0 | 1
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


# Default livestream persona (Vietnamese MC). Overridable via LLM_SYSTEM_PROMPT.
_DEFAULT_PERSONA = (
    "Bạn là MC bán hàng livestream tiếng Việt. "
    "Trả lời ngắn gọn, nhiệt tình, tập trung sản phẩm và khuyến mãi. "
    "Từ chối lịch sự các câu hỏi không liên quan đến sản phẩm hoặc nhạy cảm."
)


@dataclass
class LLMConfig:
    """LLM engine configuration (env-driven)."""

    engine: str = "none"               # vllm | llamacpp | sglang | hf | none
    model: str = ""                     # HF model id or path
    model_path: str = ""                # local path (for llamacpp GGUF dir)
    device: str = "auto"               # cuda | cpu | auto
    max_tokens: int = 128
    temperature: float = 0.7
    system_prompt: str = _DEFAULT_PERSONA
    max_model_len: int = 4096
    quantization: Optional[str] = None  # awq | gptq | fp8 | None
    n_ctx: int = 4096                   # llamacpp context
    n_gpu_layers: int = -1              # llamacpp GPU layers
    stream: bool = False               # LLM_STREAM=1 -> emit TextChunks via stream_chunks()
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            engine=os.environ.get("LLM_ENGINE", "none").lower(),
            model=os.environ.get("LLM_MODEL", ""),
            model_path=os.environ.get("LLM_MODEL_PATH", os.environ.get("LLM_GGUF_DIR", "")),
            device=os.environ.get("LLM_DEVICE", "auto"),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "128")),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.7")),
            system_prompt=os.environ.get("LLM_SYSTEM_PROMPT", _DEFAULT_PERSONA),
            max_model_len=int(os.environ.get("LLM_MAX_MODEL_LEN", "4096")),
            quantization=os.environ.get("LLM_QUANTIZATION") or None,
            n_ctx=int(os.environ.get("LLM_N_CTX", "4096")),
            n_gpu_layers=int(os.environ.get("LLM_N_GPU_LAYERS", "-1")),
            stream=os.environ.get("LLM_STREAM", "").lower() in ("1", "true", "on", "yes"),
        )

    def to_engine_cfg(self) -> dict:
        """Build the cfg dict that core.llm.load_engine expects."""
        cfg: dict[str, Any] = {"engine": self.engine}
        if self.model:
            cfg["model"] = self.model
        if self.model_path:
            cfg["model_path"] = cfg["weights_path"] = self.model_path
        cfg["device"] = self.device
        cfg["dtype"] = os.environ.get("LLM_DTYPE", "auto")
        cfg["max_model_len"] = self.max_model_len
        cfg["max_tokens"] = self.max_tokens
        cfg["temperature"] = self.temperature
        cfg["system_prompt"] = self.system_prompt
        cfg["quantization"] = self.quantization
        cfg["n_ctx"] = self.n_ctx
        cfg["n_gpu_layers"] = self.n_gpu_layers
        cfg["stream"] = self.stream
        cfg["seed"] = int(os.environ.get("LLM_SEED", "42"))
        cfg["enable_prefix_caching"] = os.environ.get(
            "LLM_PREFIX_CACHE", "1"
        ).lower() in ("1", "true", "yes")
        cfg["gpu_memory_utilization"] = float(
            os.environ.get("LLM_GPU_MEM_UTIL", "0.9")
        )
        cfg["trust_remote_code"] = os.environ.get(
            "LLM_TRUST_REMOTE_CODE", "0"
        ).lower() in ("1", "true", "yes")
        # ── Additional optimizations (low-hanging fruit) ──
        # FP8 KV cache: halve KV VRAM → more sessions/longer context per GPU.
        # Default ON for production (Ampere+ GPUs). Set LLM_KV_CACHE_DTYPE=auto to disable.
        cfg["kv_cache_dtype"] = os.environ.get("LLM_KV_CACHE_DTYPE", "fp8")
        # Chunked prefill: reduce TTFT for long prompts (persona + catalog > 1K tokens).
        # vLLM processes prefill in chunks interleaved with decode → lower first-token latency.
        cfg["enable_chunked_prefill"] = os.environ.get(
            "LLM_CHUNKED_PREFILL", "1"
        ).lower() in ("1", "true", "yes")
        # Speculative decoding: draft model proposes tokens, main model verifies.
        # Needs a small draft model (e.g. 0.5B) → 1.5-3.6x speedup. Off by default (needs draft model).
        cfg["speculative_model"] = os.environ.get("LLM_SPECULATIVE_MODEL", "")
        # FlashAttention: vLLM auto-detects on Ampere+. Set explicitly if needed.
        # FA2: Ampere+ (A10G, L4, L40S, A100). FA3: Hopper only (H100). T4 = NOT supported.
        cfg["enforce_eager"] = os.environ.get(
            "LLM_ENFORCE_EAGER", "0"
        ).lower() in ("1", "true", "yes")
        cfg.update(self.extra)
        return cfg


@dataclass
class TTSConfig:
    """TTS engine configuration (env-driven)."""

    engine: str = "transformers"       # transformers(default) | vieneu | cosyvoice | tone
    model: str = ""                     # HF model id or path
    device: str = "auto"
    sample_rate: int = 24_000
    ref_audio: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "TTSConfig":
        return cls(
            engine=os.environ.get("TTS_ENGINE", "transformers").lower(),
            model=os.environ.get("TTS_MODEL", os.environ.get("TTS_WEIGHTS", "")),
            device=os.environ.get("TTS_DEVICE", "auto"),
            sample_rate=int(os.environ.get("TTS_SAMPLE_RATE", "24000")),
            ref_audio=os.environ.get("TTS_REF_AUDIO") or None,
        )

    def to_engine_cfg(self) -> dict:
        cfg: dict[str, Any] = {"engine": self.engine}
        if self.model:
            cfg["weights_path"] = cfg["model"] = self.model
        cfg["device"] = self.device
        cfg["sample_rate"] = self.sample_rate
        cfg["ref_audio"] = self.ref_audio
        cfg.update(self.extra)
        return cfg


@dataclass
class AppConfig:
    """Runtime configuration, read from environment."""

    # Renderer backend
    render_backend: str = "cloud"

    # Session storage
    store_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # CORS
    cors_origins: str = "*"

    # Server
    port: int = 8800

    # LiveAvatar (cloud backend)
    api_key_present: bool = False

    # Director orchestration
    director_enabled: bool = False

    # Engine configs
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            render_backend=os.environ.get("RENDER_BACKEND", "cloud").lower(),
            store_backend=os.environ.get("SESSION_STORE", "memory").lower(),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            cors_origins=os.environ.get("CORS_ORIGINS", "*"),
            port=int(os.environ.get("PORT", "8800")),
            api_key_present=bool(os.environ.get("LIVEAVATAR_API_KEY")),
            director_enabled=os.environ.get("DIRECTOR_ENABLED", "0").lower()
            in ("1", "true", "yes"),
            llm=LLMConfig.from_env(),
            tts=TTSConfig.from_env(),
        )

    def cors_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def build_store(self):
        from .store import InMemorySessionStore, RedisSessionStore

        if self.store_backend == "redis":
            return RedisSessionStore(self.redis_url)
        return InMemorySessionStore()

    def build_render_backend(self):
        if self.render_backend == "self_host":
            from .render.self_host import SelfHostRenderBackend

            return SelfHostRenderBackend()
        from .render.cloud import CloudRenderBackend

        return CloudRenderBackend()

    def build_llm_engine(self):
        """Build the LLM engine from env. Returns None if LLM_ENGINE=none."""
        if self.llm.engine in ("none", "", None):
            return None
        from .llm import load_engine

        return load_engine(self.llm.to_engine_cfg())

    def build_tts_engine(self):
        """Build the TTS engine from env. Returns None if TTS_ENGINE=tone."""
        if self.tts.engine in ("tone", "", None):
            return None
        from .tts import load_engine

        return load_engine(self.tts.to_engine_cfg())
