"""Validated avatar service configuration — engine, publishing, security."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SELF_HOST_ENGINES = frozenset({"avatarforcing"})
SERVICE_NAME = "avatar"
DEFAULT_PORT = 8080


@dataclass(frozen=True)
class EngineConfig:
    """Validated self-host avatar engine selection.

    Only the AvatarForcing engine is accepted (Task 1.33: `remote_avatar` and
    `liveavatar` as engines are rejected). Hosted adapters belong to backend.
    """

    engine: str = "none"
    model: str = ""
    weights: str = ""
    device: str = "auto"

    def __post_init__(self) -> None:
        if self.engine not in SELF_HOST_ENGINES and self.engine != "none":
            raise ValueError(
                f"AVATAR_ENGINE={self.engine!r} is not a valid self-host engine; "
                f"expected one of {sorted(SELF_HOST_ENGINES)}"
            )

    def to_cfg_dict(self) -> dict:
        return {
            "engine": self.engine,
            "model": self.model or None,
            "weights_path": self.weights or None,
            "device": self.device,
        }


@dataclass(frozen=True)
class PublishingConfig:
    """LiveKit publishing settings — credentials stay server-side."""

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    room_ttl_sec: int = 3600
    fps: int = 25

    def __post_init__(self) -> None:
        if not self.livekit_url:
            raise ValueError("LIVEKIT_URL is required for avatar publishing")
        if not self.livekit_api_key or not self.livekit_api_secret:
            raise ValueError(
                "LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required "
                "for avatar publishing"
            )
        if self.room_ttl_sec < 60:
            raise ValueError("LIVEKIT_ROOM_TTL_SEC must be >= 60")
        if self.fps < 1:
            raise ValueError("LIVEKIT_FPS must be >= 1")


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server settings."""

    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    log_level: str = "INFO"
    runtime_root: Path = Path(".runtime")
    max_body_bytes: int = 100_000
    max_concurrent_requests: int = 4
    request_timeout_sec: float = 60.0

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValueError(f"AVATAR_PORT={self.port} out of range")
        if self.max_concurrent_requests < 1:
            raise ValueError("AVATAR_MAX_CONCURRENT must be >= 1")


@dataclass(frozen=True)
class SecurityConfig:
    """Service authentication and authorization configuration."""

    auth_enabled: bool = False
    auth_token: str = ""
    admin_token: str = ""
    allowed_scopes: tuple[str, ...] = ("avatar.render", "avatar.admin")
    max_concurrent_requests: int = 4
    max_gpu_concurrent_requests: int = 1

    def __post_init__(self) -> None:
        if self.auth_enabled and not self.auth_token:
            raise ValueError("AVATAR_AUTH_TOKEN required when AVATAR_AUTH_ENABLED=1")
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
    return EngineConfig(
        engine=os.environ.get("AVATAR_ENGINE", "none").strip().lower(),
        model=os.environ.get("AVATAR_MODEL", "").strip(),
        weights=os.environ.get("AVATAR_WEIGHTS", "").strip(),
        device=os.environ.get("AVATAR_DEVICE", "auto").strip().lower(),
    )


def load_publishing_config() -> PublishingConfig:
    return PublishingConfig(
        livekit_url=os.environ.get("LIVEKIT_URL", "").strip(),
        livekit_api_key=os.environ.get("LIVEKIT_API_KEY", "").strip(),
        livekit_api_secret=os.environ.get("LIVEKIT_API_SECRET", "").strip(),
        room_ttl_sec=_parse_int(os.environ.get("LIVEKIT_ROOM_TTL_SEC"), 3600),
        fps=_parse_int(os.environ.get("LIVEKIT_FPS"), 25),
    )


def load_server_config() -> ServerConfig:
    return ServerConfig(
        host=os.environ.get("AVATAR_HOST", "0.0.0.0").strip(),
        port=_parse_int(os.environ.get("AVATAR_PORT"), DEFAULT_PORT),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        runtime_root=Path(os.environ.get("RUNTIME_ROOT", ".runtime")),
        max_body_bytes=_parse_int(os.environ.get("AVATAR_MAX_BODY_BYTES"), 100_000),
        max_concurrent_requests=_parse_int(
            os.environ.get("AVATAR_MAX_CONCURRENT"), 4
        ),
        request_timeout_sec=float(os.environ.get("AVATAR_REQUEST_TIMEOUT", "60.0")),
    )


def load_security_config() -> SecurityConfig:
    token = os.environ.get("AVATAR_AUTH_TOKEN", "").strip()
    return SecurityConfig(
        auth_enabled=_parse_bool(os.environ.get("AVATAR_AUTH_ENABLED"), bool(token)),
        auth_token=token,
        admin_token=os.environ.get("AVATAR_ADMIN_TOKEN", "").strip(),
        max_concurrent_requests=_parse_int(
            os.environ.get("AVATAR_MAX_CONCURRENT"), 4
        ),
        max_gpu_concurrent_requests=_parse_int(
            os.environ.get("AVATAR_MAX_GPU_CONCURRENT"), 1
        ),
    )