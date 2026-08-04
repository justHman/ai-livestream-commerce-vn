"""Portable runtime config — env-driven, same code Colab -> AWS (OpenSpec 1.21 copy).

12-factor: every deployment-specific value comes from an environment variable.
Copied from ``core/config.py`` (COPY-DON'T-IMPORT) so the canonical backend
service is self-contained. The core module is frozen and will be deleted after
the whole refactor completes.

Engine selection (the production seam):
  RENDER_BACKEND   cloud_liveavatar | self_host_avatarforcing_half |
                   self_host_echoavatar_full | mock
  SESSION_STORE    memory(Colab) | redis(AWS)
  LLM_ENGINE       vllm | llamacpp | sglang | hf | none(default echo)
  TTS_ENGINE       transformers(default) | vieneu | cosyvoice | tone
  DIRECTOR_ENABLED 0 | 1

Prompt ownership (OpenSpec 1.11): canonical Director prompt text lives in the
Markdown bundle under ``backend/application/director/prompts/``. This module
keeps only non-prompt config values and exposes loader-backed compatibility
symbols (``BASE_SALE_PERSONA``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


def _canonical_base_sales() -> str:
    """Compatibility seam: canonical base sales persona text from the bundle.

    The legacy ``BASE_SALE_PERSONA`` string combined the persona and the
    response-guardrail text in one literal. The canonical bundle splits them
    into ``base_sales_vi.md`` and ``response_guardrails_vi.md``; recompose them
    here so legacy callers observe the same semantic content.
    """
    from backend.application.director.prompts.loader import load_bundle

    bundle = load_bundle()
    base = bundle.prompt("base_sales_vi")
    guardrails = bundle.prompt("response_guardrails_vi")
    return base + "\n\n" + guardrails


# Default shop profile (thay bằng thông tin shop thật qua env SHOP_PROFILE).
_DEFAULT_SHOP_PROFILE = (
    "Thông tin shop:\n"
    "- Tên shop: Chưa cấu hình (set env SHOP_PROFILE)\n"
    "- Liên hệ: Chưa cấu hình\n"
    "Khi viewer hỏi thông tin shop (địa chỉ, SĐT, liên hệ), dùng SHOP_PROFILE trên."
)

# Backward-compatible constant — loader-backed seam over the canonical bundle.
BASE_SALE_PERSONA = _canonical_base_sales()

# Composed persona (base + shop profile). Overridable via LLM_SYSTEM_PROMPT.
_DEFAULT_PERSONA = BASE_SALE_PERSONA + "\n\n" + _DEFAULT_SHOP_PROFILE


def _build_persona(shop_profile: str = "") -> str:
    """Compose system prompt: canonical base persona + guardrails + shop profile.

    - LLM_SYSTEM_PROMPT (if set) overrides only the base persona, NEVER the
      guardrails — guardrails always come from the canonical Markdown bundle.
    - shop_profile (if passed via session attach from FE) is the per-shop info
      (name, address, phone, ...). Falls back to env SHOP_PROFILE or default.
    - Canonical base and guardrails come from the tracked Markdown bundle.
    """
    from backend.application.director.prompts.loader import load_bundle

    bundle = load_bundle()
    base = os.environ.get("LLM_SYSTEM_PROMPT") or bundle.prompt("base_sales_vi")
    guardrails = bundle.prompt("response_guardrails_vi")
    shop = shop_profile or os.environ.get("SHOP_PROFILE") or _DEFAULT_SHOP_PROFILE
    return base + "\n\n" + guardrails + "\n\n" + shop


@dataclass
class LLMConfig:
    """LLM engine configuration (env-driven)."""

    engine: str = "none"  # vllm | llamacpp | sglang | hf | openai_compat | none
    model: str = ""  # HF model id or path
    model_path: str = ""  # local path (for llamacpp GGUF dir)
    device: str = "auto"  # cuda | cpu | auto
    max_tokens: int = 128
    temperature: float = 0.7
    system_prompt: str = _DEFAULT_PERSONA
    max_model_len: int = 4096
    quantization: Optional[str] = None  # awq | gptq | fp8 | None
    n_ctx: int = 4096  # llamacpp context
    n_gpu_layers: int = -1  # llamacpp GPU layers
    stream: bool = False  # LLM_STREAM=1 -> emit TextChunks via stream_chunks()
    base_url: str = ""  # remote OpenAI-compat endpoint (LLM_BASE_URL)
    guided_json: bool = False  # LLM_GUIDED_JSON / outlines structured output
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
            max_model_len=int(os.environ.get("LLM_MAX_MODEL_LEN", "4096")),
            quantization=os.environ.get("LLM_QUANTIZATION") or None,
            n_ctx=int(os.environ.get("LLM_N_CTX", "4096")),
            n_gpu_layers=int(os.environ.get("LLM_N_GPU_LAYERS", "-1")),
            stream=os.environ.get("LLM_STREAM", "").lower() in ("1", "true", "on", "yes"),
            base_url=os.environ.get("LLM_BASE_URL", ""),
            guided_json=os.environ.get("LLM_GUIDED_JSON", "0").lower()
            in ("1", "true", "on", "yes"),
            system_prompt=_build_persona(),
        )

    def to_engine_cfg(self) -> dict:
        """Build the cfg dict that llm.engines.base.load_engine expects."""
        cfg: dict[str, Any] = {"engine": self.engine}
        if self.model:
            cfg["model"] = self.model
        if self.model_path:
            cfg["model_path"] = cfg["weights_path"] = self.model_path
        if self.base_url:
            cfg["base_url"] = self.base_url
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
        cfg["guided_json"] = self.guided_json
        cfg["seed"] = int(os.environ.get("LLM_SEED", "42"))
        cfg["enable_prefix_caching"] = os.environ.get("LLM_PREFIX_CACHE", "1").lower() in (
            "1",
            "true",
            "yes",
        )
        cfg["gpu_memory_utilization"] = float(os.environ.get("LLM_GPU_MEM_UTIL", "0.9"))
        cfg["trust_remote_code"] = os.environ.get("LLM_TRUST_REMOTE_CODE", "0").lower() in (
            "1",
            "true",
            "yes",
        )
        # ── Additional optimizations (low-hanging fruit) ──
        # FP8 KV cache: halve KV VRAM → more sessions/longer context per GPU.
        # Default ON for production (Ampere+ GPUs). Set LLM_KV_CACHE_DTYPE=auto to disable.
        cfg["kv_cache_dtype"] = os.environ.get("LLM_KV_CACHE_DTYPE", "fp8")
        # Chunked prefill: reduce TTFT for long prompts (persona + catalog > 1K tokens).
        # vLLM processes prefill in chunks interleaved with decode → lower first-token latency.
        cfg["enable_chunked_prefill"] = os.environ.get("LLM_CHUNKED_PREFILL", "1").lower() in (
            "1",
            "true",
            "yes",
        )
        # Speculative decoding: draft model proposes tokens, main model verifies.
        # Needs a small draft model (e.g. 0.5B) → 1.5-3.6x speedup. Off by default (needs draft model).
        cfg["speculative_model"] = os.environ.get("LLM_SPECULATIVE_MODEL", "")
        # FlashAttention: vLLM auto-detects on Ampere+. Set explicitly if needed.
        # FA2: Ampere+ (A10G, L4, L40S, A100). FA3: Hopper only (H100). T4 = NOT supported.
        cfg["enforce_eager"] = os.environ.get("LLM_ENFORCE_EAGER", "0").lower() in (
            "1",
            "true",
            "yes",
        )
        cfg.update(self.extra)
        return cfg


@dataclass
class TTSConfig:
    """TTS engine configuration (env-driven).

    Two layers:
      - ``preset_id`` (default ``vieneu-v3-turbo``) is the recommended preset
        for the frontend dropdown's INITIAL selection (Phase A). It is a
        passive hint; setting it does NOT load that engine at startup.
      - ``engine``/``model``/``sample_rate`` (default ``transformers``/empty/
        24 kHz) drive the engine that is actually built at startup. If
        ``TTS_PRESET_ID`` is set to a known preset id, its engine/weights/
        sample_rate WIN over the individual TTS_ENGINE/TTS_WEIGHTS/
        TTS_SAMPLE_RATE env vars so a single env switch picks both the
        dropdown default AND the loaded engine.
    """

    engine: str = "transformers"  # transformers(default) | vieneu | cosyvoice | tone | remote_http
    model: str = ""  # HF model id or path
    device: str = "auto"
    sample_rate: int = 24_000
    ref_audio: Optional[str] = None
    preset_id: str = "vieneu-v3-turbo"
    base_url: str = ""  # remote TTS service URL (TTS_BASE_URL)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "TTSConfig":
        preset_id = os.environ.get("TTS_PRESET_ID", "vieneu-v3-turbo")
        # Default fields (preserve offline behaviour when no TTS_* env is set).
        engine = os.environ.get("TTS_ENGINE", "transformers").lower()
        model = os.environ.get("TTS_MODEL", os.environ.get("TTS_WEIGHTS", ""))
        sample_rate = int(os.environ.get("TTS_SAMPLE_RATE", "24000"))
        device = os.environ.get("TTS_DEVICE", "auto")

        # If TTS_PRESET_ID is explicitly set in env AND it matches a registered
        # preset, the preset WINS over the individual fields (Phase A spec).
        # Lazy import to avoid circular dependency at module import time.
        if "TTS_PRESET_ID" in os.environ:
            try:
                from .engine_manager import get_tts_preset  # local import: avoid cycles

                preset = get_tts_preset(preset_id)
            except Exception:
                preset = None
            if preset is not None:
                engine = preset["engine"]
                model = preset["weights_path"]
                sample_rate = preset["sample_rate"]
                device = preset["device"]

        return cls(
            engine=engine,
            model=model,
            device=device,
            sample_rate=sample_rate,
            ref_audio=os.environ.get("TTS_REF_AUDIO") or None,
            preset_id=preset_id,
            base_url=os.environ.get("TTS_BASE_URL", ""),
            extra={
                # ElevenLabs remote TTS (Stage 2 ship-fast): api_key + voice_id + model_id.
                "api_key": os.environ.get("TTS_API_KEY")
                or os.environ.get("ELEVENLABS_API_KEY", ""),
                "voice_id": os.environ.get("TTS_VOICE_ID", ""),
                "model_id": os.environ.get("TTS_MODEL_ID", ""),
            },
        )

    def to_engine_cfg(self) -> dict:
        cfg: dict[str, Any] = {"engine": self.engine}
        if self.model:
            cfg["weights_path"] = cfg["model"] = self.model
        if self.base_url:
            cfg["base_url"] = self.base_url
        cfg["device"] = self.device
        cfg["sample_rate"] = self.sample_rate
        cfg["ref_audio"] = self.ref_audio
        cfg.update(self.extra)
        return cfg


@dataclass
class AppConfig:
    """Runtime configuration, read from environment."""

    # Deployment environment: "dev" (auth disabled when tokens empty) | "prod"
    app_env: str = "dev"

    # Renderer backend
    render_backend: str = "cloud_liveavatar"

    # Session storage
    store_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # CORS
    cors_origins: str = "*"

    # Server
    port: int = 8800
    max_request_body_bytes: int = 65_536
    api_rate_limit_requests: int = 30
    api_rate_limit_window_seconds: float = 60.0
    api_rate_limit_max_keys: int = 10_000
    ws_rate_limit_messages: int = 60
    ws_rate_limit_window_seconds: float = 60.0

    # LiveAvatar (cloud backend)
    api_key_present: bool = False

    # Mock avatar renderer (RENDER_BACKEND=mock)
    mock_avatar_fps: int = 25
    mock_avatar_width: int = 640
    mock_avatar_height: int = 360

    # Streaming coordinator (Task 8). Bounded VideoWindow queue cap + the
    # TextChunker defaults used by the LLM->TTS->avatar pipeline.
    avatar_max_queue_windows: int = 5
    text_chunk_min_chars: int = 12
    text_chunk_target_chars: int = 40
    text_chunk_max_chars: int = 80
    text_chunk_flush_timeout_ms: int = 350

    # Director orchestration
    director_enabled: bool = False

    # Auth tokens (Task 7). Empty + app_env="dev" -> auth disabled.
    backend_api_token: str = ""  # viewer token: /lite/* + /ws/control
    admin_api_token: str = ""  # admin token: /engines/* + /debug/*

    # Debug mode gate (Task 7). When False, /debug/* -> 404.
    debug_enabled: bool = False

    # Remote service URLs (AWS multi-service; empty offline)
    avatar_base_url: str = ""
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    lmcache_enabled: bool = False
    # Pipecat orchestration toggle (full wiring is another agent). Default off.
    pipecat_enabled: bool = False
    # Director coverage match threshold (speech vs key_selling_points)
    coverage_match_threshold: float = 0.75
    # Optional Postgres runtime (Wave D). Empty = store disabled.
    database_url: str = ""
    # Backend audio publish to LiveKit SFU (stub until SDK wired).
    livekit_publish: bool = False

    # Engine configs
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            app_env=os.environ.get("APP_ENV", "dev").lower(),
            render_backend=os.environ.get("RENDER_BACKEND", "cloud_liveavatar").lower(),
            store_backend=os.environ.get("SESSION_STORE", "memory").lower(),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            cors_origins=os.environ.get("CORS_ORIGINS", "*"),
            port=int(os.environ.get("PORT", "8800")),
            max_request_body_bytes=int(os.environ.get("MAX_REQUEST_BODY_BYTES", "65536")),
            api_rate_limit_requests=int(os.environ.get("API_RATE_LIMIT_REQUESTS", "30")),
            api_rate_limit_window_seconds=float(
                os.environ.get("API_RATE_LIMIT_WINDOW_SECONDS", "60")
            ),
            api_rate_limit_max_keys=int(os.environ.get("API_RATE_LIMIT_MAX_KEYS", "10000")),
            ws_rate_limit_messages=int(os.environ.get("WS_RATE_LIMIT_MESSAGES", "60")),
            ws_rate_limit_window_seconds=float(
                os.environ.get("WS_RATE_LIMIT_WINDOW_SECONDS", "60")
            ),
            api_key_present=bool(os.environ.get("LIVEAVATAR_API_KEY")),
            mock_avatar_fps=int(os.environ.get("MOCK_AVATAR_TARGET_FPS", "25")),
            mock_avatar_width=int(os.environ.get("MOCK_AVATAR_WIDTH", "640")),
            mock_avatar_height=int(os.environ.get("MOCK_AVATAR_HEIGHT", "360")),
            avatar_max_queue_windows=int(os.environ.get("AVATAR_MAX_QUEUE_WINDOWS", "5")),
            text_chunk_min_chars=int(os.environ.get("TEXT_CHUNK_MIN_CHARS", "12")),
            text_chunk_target_chars=int(os.environ.get("TEXT_CHUNK_TARGET_CHARS", "40")),
            text_chunk_max_chars=int(os.environ.get("TEXT_CHUNK_MAX_CHARS", "80")),
            text_chunk_flush_timeout_ms=int(os.environ.get("TEXT_CHUNK_FLUSH_TIMEOUT_MS", "350")),
            director_enabled=os.environ.get("DIRECTOR_ENABLED", "0").lower()
            in ("1", "true", "yes"),
            backend_api_token=os.environ.get("BACKEND_API_TOKEN", ""),
            admin_api_token=os.environ.get("ADMIN_API_TOKEN", ""),
            debug_enabled=os.environ.get("DEBUG_ENABLED", "0").lower()
            in ("1", "true", "on", "yes"),
            avatar_base_url=os.environ.get("AVATAR_BASE_URL", ""),
            livekit_url=os.environ.get("LIVEKIT_URL", ""),
            livekit_api_key=os.environ.get("LIVEKIT_API_KEY", ""),
            livekit_api_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
            lmcache_enabled=os.environ.get("LMCACHE_ENABLED", "0").lower()
            in ("1", "true", "on", "yes"),
            pipecat_enabled=os.environ.get("PIPECAT_ENABLED", "0").lower()
            in ("1", "true", "on", "yes"),
            coverage_match_threshold=float(os.environ.get("COVERAGE_MATCH_THRESHOLD", "0.75")),
            database_url=os.environ.get("DATABASE_URL", ""),
            livekit_publish=os.environ.get("LIVEKIT_PUBLISH", "0").lower()
            in ("1", "true", "on", "yes"),
            llm=LLMConfig.from_env(),
            tts=TTSConfig.from_env(),
        )

    def cors_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def build_store(self):
        from backend.application.db import InMemorySessionStore, RedisSessionStore

        if self.store_backend == "redis":
            return RedisSessionStore(self.redis_url)
        return InMemorySessionStore()

    def build_render_backend(self):
        """Build the canonical offline renderer for this backend service.

        The mock renderer lives in the canonical avatar_service
        (``avatar.engines.mock``) — the media plane owns all render backends.
        Cloud (``cloud_liveavatar``) and self-host backends are the
        avatar_service's domain and are reached through the HTTP transport
        seam (``backend.application.clients.avatar``); this control plane
        builds only what it can run offline.
        """
        if self.render_backend == "mock":
            from avatar.engines.mock import MockRenderBackend

            return MockRenderBackend(
                fps=self.mock_avatar_fps,
                width=self.mock_avatar_width,
                height=self.mock_avatar_height,
            )
        if self.render_backend in (
            "cloud_liveavatar",
            "self_host_avatarforcing_half",
            "self_host_echoavatar_full",
        ):
            from avatar.engines.base import RenderBackend, StartOptions, StartResult
            from avatar.engines.windows import AudioWindow

            class _RemoteBackend(RenderBackend):
                """Placeholder: cloud/self-host rendering runs in avatar_service.

                The control plane must not embed LiveAvatar SDK code; the
                avatar_service exposes /start /stop /audio over HTTP, and
                ``application.clients.avatar`` implements the thin transport.
                """

                name: str = self.render_backend

                def start(self, opts: StartOptions) -> StartResult:
                    raise RuntimeError(
                        f"{self.name} runs in avatar_service; use the HTTP transport seam"
                    )

                def stream_audio(self, session_id: str, window: AudioWindow):
                    raise RuntimeError(
                        f"{self.name} runs in avatar_service; use the HTTP transport seam"
                    )

                def stop(self, session_id: str) -> None:  # pragma: no cover
                    pass

                def interrupt(self, session_id: str) -> None:  # pragma: no cover
                    pass

            return _RemoteBackend()
        raise ValueError(
            "unknown RENDER_BACKEND "
            f"{self.render_backend!r}; expected cloud_liveavatar, "
            "self_host_avatarforcing_half, self_host_echoavatar_full, or mock"
        )

    def build_llm_engine(self):
        """Build the LLM engine from env. Returns None if LLM_ENGINE=none."""
        if self.llm.engine in ("none", "", None):
            return None
        from llm.engines.base import load_engine

        return load_engine(self.llm.to_engine_cfg())

    def build_tts_engine(self):
        """Build the TTS engine from env. Returns None if TTS_ENGINE=tone."""
        if self.tts.engine in ("tone", "", None):
            return None
        from tts.engines.base import load_engine

        return load_engine(self.tts.to_engine_cfg())


__all__ = ["AppConfig", "LLMConfig", "TTSConfig", "BASE_SALE_PERSONA", "_build_persona"]
