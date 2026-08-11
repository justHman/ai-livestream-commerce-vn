"""Validated TTS service configuration — engine, server, security, limits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SELF_HOST_ENGINES = frozenset({"vieneu", "cosyvoice"})
SERVICE_NAME = "tts"
DEFAULT_PORT = 8002

# Change T: provider-neutral runtime defaults (cluster 1.5). These configure
# the scheduler/provider runtime introduced by Change T; the legacy EngineConfig
# below keeps driving the current engines/ path until the runtime lands.
DEFAULT_TTS_PROVIDER = "vieneu_v3"
DEFAULT_TTS_MODEL_REVISION = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
DEFAULT_TTS_RESPONSE_FORMAT = "wav"
ACCELERATORS = ("auto", "cpu", "gpu")
RESPONSE_FORMATS = ("pcm", "wav")
# Voice enrollment bounds (task 5.1): reference WAVs are bounded before the
# provider encodes them. 10 MB / 30 s match the SDK's prepare_reference trim.
DEFAULT_VOICE_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_VOICE_MAX_SECONDS = 30


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
class RuntimeConfig:
    """Provider-neutral TTS runtime settings for the Change T scheduler.

    Declares the provider, accelerator, model revision, response format,
    scheduler admission bounds, and voice-profile store location. Validation
    fails fast on bad env so misconfiguration surfaces at startup, not under
    load.
    """

    provider: str = DEFAULT_TTS_PROVIDER
    accelerator: str = "auto"
    model_revision: str = DEFAULT_TTS_MODEL_REVISION
    response_format: str = DEFAULT_TTS_RESPONSE_FORMAT
    global_pending_limit: int = 512
    per_session_pending_limit: int = 64
    request_deadline_ms: int = 30_000
    max_batch_size: int = 32
    coalesce_window_ms: int = 10
    aging_threshold_ms: int = 5_000
    voice_store_uri: str = ""
    voice_max_bytes: int = DEFAULT_VOICE_MAX_BYTES
    voice_max_seconds: int = DEFAULT_VOICE_MAX_SECONDS

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("TTS_PROVIDER must not be empty")
        if self.accelerator not in ACCELERATORS:
            raise ValueError(
                f"TTS_ACCELERATOR={self.accelerator!r} is not valid; "
                f"expected one of {sorted(ACCELERATORS)}"
            )
        if not self.model_revision:
            raise ValueError("TTS_MODEL_REVISION must not be empty")
        if self.response_format not in RESPONSE_FORMATS:
            raise ValueError(
                f"TTS_RESPONSE_FORMAT={self.response_format!r} is not valid; "
                f"expected one of {sorted(RESPONSE_FORMATS)}"
            )
        for name, value in (
            ("TTS_GLOBAL_PENDING_LIMIT", self.global_pending_limit),
            ("TTS_PER_SESSION_PENDING_LIMIT", self.per_session_pending_limit),
            ("TTS_REQUEST_DEADLINE_MS", self.request_deadline_ms),
            ("TTS_MAX_BATCH_SIZE", self.max_batch_size),
            ("TTS_COALESCE_WINDOW_MS", self.coalesce_window_ms),
            ("TTS_AGING_THRESHOLD_MS", self.aging_threshold_ms),
            ("TTS_VOICE_MAX_BYTES", self.voice_max_bytes),
            ("TTS_VOICE_MAX_SECONDS", self.voice_max_seconds),
        ):
            if value < 1:
                raise ValueError(f"{name} must be >= 1")


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


def load_runtime_config() -> RuntimeConfig:
    """Build and validate runtime config from environment variables."""
    voice_store = os.environ.get("TTS_VOICE_STORE_URI", "").strip()
    if not voice_store:
        runtime_root = Path(os.environ.get("RUNTIME_ROOT", ".runtime"))
        voice_store = f"file://{(runtime_root / 'voice_profiles').as_posix()}"
    return RuntimeConfig(
        provider=os.environ.get("TTS_PROVIDER", DEFAULT_TTS_PROVIDER).strip().lower(),
        accelerator=os.environ.get("TTS_ACCELERATOR", "auto").strip().lower(),
        model_revision=os.environ.get("TTS_MODEL_REVISION", DEFAULT_TTS_MODEL_REVISION).strip(),
        response_format=os.environ.get("TTS_RESPONSE_FORMAT", DEFAULT_TTS_RESPONSE_FORMAT)
        .strip()
        .lower(),
        global_pending_limit=_parse_int(os.environ.get("TTS_GLOBAL_PENDING_LIMIT"), 512),
        per_session_pending_limit=_parse_int(os.environ.get("TTS_PER_SESSION_PENDING_LIMIT"), 64),
        request_deadline_ms=_parse_int(os.environ.get("TTS_REQUEST_DEADLINE_MS"), 30_000),
        max_batch_size=_parse_int(os.environ.get("TTS_MAX_BATCH_SIZE"), 32),
        coalesce_window_ms=_parse_int(os.environ.get("TTS_COALESCE_WINDOW_MS"), 10),
        aging_threshold_ms=_parse_int(os.environ.get("TTS_AGING_THRESHOLD_MS"), 5_000),
        voice_store_uri=voice_store,
        voice_max_bytes=_parse_int(os.environ.get("TTS_VOICE_MAX_BYTES"), DEFAULT_VOICE_MAX_BYTES),
        voice_max_seconds=_parse_int(
            os.environ.get("TTS_VOICE_MAX_SECONDS"), DEFAULT_VOICE_MAX_SECONDS
        ),
    )
