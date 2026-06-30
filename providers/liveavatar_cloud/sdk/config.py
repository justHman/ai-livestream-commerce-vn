"""Portable config — env-driven, same code Colab → AWS.

12-factor: all deployment-specific values come from environment variables.
Colab sets them inline; AWS sets them via task definition / secrets manager.
Nothing here is hardcoded to one environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    """Runtime configuration, read from environment."""

    # Session storage backend: "memory" (Colab) | "redis" (AWS)
    store_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # CORS — "*" for dev/Colab; comma-separated origins for production
    cors_origins: str = "*"

    # Server
    port: int = 8800

    # LiveAvatar
    api_key_present: bool = False

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            store_backend=os.environ.get("SESSION_STORE", "memory").lower(),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            cors_origins=os.environ.get("CORS_ORIGINS", "*"),
            port=int(os.environ.get("PORT", "8800")),
            api_key_present=bool(os.environ.get("LIVEAVATAR_API_KEY")),
        )

    def cors_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def build_store(self):
        """Instantiate the configured SessionStore."""
        from ..service.store import InMemorySessionStore, RedisSessionStore

        if self.store_backend == "redis":
            return RedisSessionStore(self.redis_url)
        return InMemorySessionStore()
