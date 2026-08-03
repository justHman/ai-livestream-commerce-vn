"""Service authentication configuration shared by the avatar security modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SecurityConfig:
    """Authentication, authorization scope, and concurrency limits."""

    auth_enabled: bool = False
    auth_token: str = ""
    admin_token: str = ""
    allowed_scopes: tuple[str, ...] = ("avatar.render", "avatar.admin")
    max_concurrent_requests: int = 4
    max_gpu_concurrent_requests: int = 1

    def __post_init__(self) -> None:
        if self.auth_enabled and not self.auth_token:
            raise ValueError("auth_token required when auth_enabled")
        if self.max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be >= 1")
        if self.max_gpu_concurrent_requests < 1:
            raise ValueError("max_gpu_concurrent_requests must be >= 1")
