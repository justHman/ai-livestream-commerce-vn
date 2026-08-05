"""Validated TTS service configuration — engine, server, security, limits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SELF_HOST_ENGINES = frozenset({"vieneu", "cosyvoice"})
SERVICE_NAME = "tts"
DEFAULT_PORT = 8002


@dataclass(frozen=True)
class EngineConfig:
    """Validated self-host TTS engine selection and runtime settings.

    Only self-host engines are accepted (Task 1.33: `remote_http` as an
    engine is rejected). Service never selects hosted adapters; the backend
    owns adapter selection.
    """

    engine: str = "none"
    model: str = ""
    model_path: str = ""
    ref_audio: Optional[str] = None
    device: str = "auto"
    sample_rate: int = 24_000

    def __post_init__(self) -> None:
        if self.engine not in SELF_HOST_ENGINES and self.engine != "none":
            raise ValueError(
                f"TTS_ENGINE={self.engine!r} is not a valid self-host engine; "
                f"expected one of {sorted(SELF_HOST_ENGINES)}"
            )
        if self.sample_rate <= 0:
            raise ValueError("TTS_SAMPLE_RATE must be > 0")

    def to_cfg_dict(self) -> dict:
        """Build the engine-agnostic config dict consumed by `load_engine`."""
        return {
            "engine": self.engine,
            "model": self.model or None,
            "weights_path": self.model_path or None,
            "ref_audio": self.ref_audio,
            "device": self.device,
            "sample_rate": self.sample_rate,
        }


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server settings."""

    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    log_level: str = "INFO"
    runtime_root: Path = Path(".runtime")
    max_body_bytes: int = 200_000
    max_concurrent_requests: int = 4
    request_timeout_sec: float = 120.0

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValueError(f"TTS_PORT={self.port} out of range")
        if self.max_body_bytes < 1:
            raise ValueError("TTS_MAX_BODY_BYTES must be >= 1")
        if self.max_concurrent_requests < 1:
            raise ValueError("TTS_MAX_CONCURRENT must be >= 1")


@dataclass(frozen=True)
class SecurityConfig:
    """Service authentication and authorization configuration."""

    auth_enabled: bool = False
    auth_token: str = ""
    admin_token: str = ""
    allowed_scopes: tuple[str, ...] = ("tts.synthesis", "tts.voices")
    max_concurrent_requests: int = 4
    max_gpu_concurrent_requests: int = 1

    def __post_init__(self) -> None:
        if self.auth_enabled and not self.auth_token:
            raise ValueError("TTS_AUTH_TOKEN required when TTS_AUTH_ENABLED=1")
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


def load_engine_config() -> EngineConfig:
    """Build and validate engine config from environment variables."""
    return EngineConfig(
        engine=os.environ.get("TTS_ENGINE", "none").strip().lower(),
        model=os.environ.get("TTS_MODEL", "").strip(),
        model_path=os.environ.get("TTS_WEIGHTS_PATH", "").strip(),
        ref_audio=os.environ.get("TTS_REF_AUDIO") or None,
        device=os.environ.get("TTS_DEVICE", "auto").strip().lower(),
        sample_rate=_parse_int(os.environ.get("TTS_SAMPLE_RATE"), 24_000),
    )


def load_server_config() -> ServerConfig:
    """Build and validate server config from environment variables."""
    return ServerConfig(
        host=os.environ.get("TTS_HOST", "0.0.0.0").strip(),
        port=_parse_int(os.environ.get("TTS_PORT"), DEFAULT_PORT),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        runtime_root=Path(os.environ.get("RUNTIME_ROOT", ".runtime")),
        max_body_bytes=_parse_int(os.environ.get("TTS_MAX_BODY_BYTES"), 200_000),
        max_concurrent_requests=_parse_int(os.environ.get("TTS_MAX_CONCURRENT"), 4),
        request_timeout_sec=float(os.environ.get("TTS_REQUEST_TIMEOUT", "120.0")),
    )


def load_security_config() -> SecurityConfig:
    """Build and validate security config from environment variables."""
    token = os.environ.get("TTS_AUTH_TOKEN", "").strip()
    return SecurityConfig(
        auth_enabled=_parse_bool(os.environ.get("TTS_AUTH_ENABLED"), bool(token)),
        auth_token=token,
        admin_token=os.environ.get("TTS_ADMIN_TOKEN", "").strip(),
        max_concurrent_requests=_parse_int(os.environ.get("TTS_MAX_CONCURRENT"), 4),
        max_gpu_concurrent_requests=_parse_int(os.environ.get("TTS_MAX_GPU_CONCURRENT"), 1),
    )
