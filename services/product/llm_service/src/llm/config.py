"""Validated LLM service configuration — engine, server, security, limits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SELF_HOST_ENGINES = frozenset({"vllm", "sglang", "transformers"})
SERVICE_NAME = "llm"
DEFAULT_PORT = 8001


@dataclass(frozen=True)
class EngineConfig:
    """Validated self-host engine selection and runtime settings.

    Only self-host engines are accepted (Task 1.33: `openai_compat` as an
    engine is rejected). Service never selects hosted adapters; the backend
    owns adapter selection.
    """

    engine: str = "none"
    model: str = ""
    model_path: str = ""
    device: str = "auto"
    max_model_len: int = 4096
    gpu_memory_utilization: float = 0.6
    dtype: str = "auto"
    quantization: Optional[str] = None
    max_num_seqs: int = 64
    seed: int = 42
    enforce_eager: bool = False
    enable_prefix_caching: bool = True

    def __post_init__(self) -> None:
        if self.engine not in SELF_HOST_ENGINES and self.engine != "none":
            raise ValueError(
                f"LLM_ENGINE={self.engine!r} is not a valid self-host engine; "
                f"expected one of {sorted(SELF_HOST_ENGINES)}"
            )
        if self.engine != "none" and not (self.model or self.model_path):
            raise ValueError(f"LLM_ENGINE={self.engine} requires LLM_MODEL or LLM_WEIGHTS_PATH")
        if self.max_model_len < 1:
            raise ValueError("LLM_MAX_MODEL_LEN must be >= 1")
        if not (0.0 < self.gpu_memory_utilization <= 1.0):
            raise ValueError("LLM_GPU_MEMORY_UTILIZATION must be in (0, 1]")

    def to_cfg_dict(self) -> dict:
        """Build the engine-agnostic config dict consumed by `load_engine`."""
        return {
            "engine": self.engine,
            "model": self.model or None,
            "weights_path": self.model_path or None,
            "device": self.device,
            "max_model_len": self.max_model_len,
            "gpu_memory_utilization": self.gpu_memory_utilization,
            "dtype": self.dtype,
            "quantization": self.quantization,
            "max_num_seqs": self.max_num_seqs,
            "seed": self.seed,
            "enforce_eager": self.enforce_eager,
            "enable_prefix_caching": self.enable_prefix_caching,
        }


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server settings."""

    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    log_level: str = "INFO"
    runtime_root: Path = Path(".runtime")
    max_body_bytes: int = 100_000
    max_concurrent_requests: int = 8
    request_timeout_sec: float = 300.0

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValueError(f"LLM_PORT={self.port} out of range")
        if self.max_body_bytes < 1:
            raise ValueError("LLM_MAX_BODY_BYTES must be >= 1")
        if self.max_concurrent_requests < 1:
            raise ValueError("LLM_MAX_CONCURRENT must be >= 1")


@dataclass(frozen=True)
class SecurityConfig:
    """Service authentication and authorization configuration."""

    auth_enabled: bool = False
    auth_token: str = ""
    admin_token: str = ""
    allowed_scopes: tuple[str, ...] = ("llm.inference", "llm.models")
    max_concurrent_requests: int = 8
    max_gpu_concurrent_requests: int = 1

    def __post_init__(self) -> None:
        if self.auth_enabled and not self.auth_token:
            raise ValueError("LLM_AUTH_TOKEN required when LLM_AUTH_ENABLED=1")
        if self.max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be >= 1")
        if self.max_gpu_concurrent_requests < 1:
            raise ValueError("max_gpu_concurrent_requests must be >= 1")


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _parse_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def load_engine_config() -> EngineConfig:
    """Build and validate engine config from environment variables."""
    return EngineConfig(
        engine=os.environ.get("LLM_ENGINE", "none").strip().lower(),
        model=os.environ.get("LLM_MODEL", "").strip(),
        model_path=os.environ.get("LLM_WEIGHTS_PATH", "").strip(),
        device=os.environ.get("LLM_DEVICE", "auto").strip().lower(),
        max_model_len=_parse_int(os.environ.get("LLM_MAX_MODEL_LEN"), 4096),
        gpu_memory_utilization=_parse_float(os.environ.get("LLM_GPU_MEMORY_UTILIZATION"), 0.6),
        dtype=os.environ.get("LLM_DTYPE", "auto").strip().lower(),
        quantization=os.environ.get("LLM_QUANTIZATION") or None,
        max_num_seqs=_parse_int(os.environ.get("LLM_MAX_NUM_SEQS"), 64),
        seed=_parse_int(os.environ.get("LLM_SEED"), 42),
        enforce_eager=_parse_bool(os.environ.get("LLM_ENFORCE_EAGER")),
        enable_prefix_caching=_parse_bool(os.environ.get("LLM_ENABLE_PREFIX_CACHING"), True),
    )


def load_server_config() -> ServerConfig:
    """Build and validate server config from environment variables."""
    return ServerConfig(
        host=os.environ.get("LLM_HOST", "0.0.0.0").strip(),
        port=_parse_int(os.environ.get("LLM_PORT"), DEFAULT_PORT),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        runtime_root=Path(os.environ.get("RUNTIME_ROOT", ".runtime")),
        max_body_bytes=_parse_int(os.environ.get("LLM_MAX_BODY_BYTES"), 100_000),
        max_concurrent_requests=_parse_int(os.environ.get("LLM_MAX_CONCURRENT"), 8),
        request_timeout_sec=_parse_float(os.environ.get("LLM_REQUEST_TIMEOUT"), 300.0),
    )


def load_security_config() -> SecurityConfig:
    """Build and validate security config from environment variables."""
    token = os.environ.get("LLM_AUTH_TOKEN", "").strip()
    return SecurityConfig(
        auth_enabled=_parse_bool(os.environ.get("LLM_AUTH_ENABLED"), bool(token)),
        auth_token=token,
        admin_token=os.environ.get("LLM_ADMIN_TOKEN", "").strip(),
        max_concurrent_requests=_parse_int(os.environ.get("LLM_MAX_CONCURRENT"), 8),
        max_gpu_concurrent_requests=_parse_int(os.environ.get("LLM_MAX_GPU_CONCURRENT"), 1),
    )
