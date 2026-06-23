"""Portable runtime config — env-driven, same code Colab -> AWS.

12-factor: every deployment-specific value comes from an environment variable.
Colab sets them inline; AWS sets them via task definition / secrets manager.

Adds RENDER_BACKEND so the renderer (cloud vs self-host) is selectable without
touching the API layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    """Runtime configuration, read from environment."""

    # Renderer backend: "cloud" (LiveAvatar) | "self_host" (future diffusion)
    render_backend: str = "cloud"

    # Session storage backend: "memory" (Colab) | "redis" (AWS)
    store_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # CORS — "*" for dev/Colab; comma-separated origins for production
    cors_origins: str = "*"

    # Server
    port: int = 8800

    # LiveAvatar (cloud backend)
    api_key_present: bool = False

    # Director orchestration layer (cluster/score/FSM). Off by default so the
    # raw say-loop works without loading an embedder.
    director_enabled: bool = False

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
        )

    def cors_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def build_store(self):
        """Instantiate the configured SessionStore."""
        from .store import InMemorySessionStore, RedisSessionStore

        if self.store_backend == "redis":
            return RedisSessionStore(self.redis_url)
        return InMemorySessionStore()

    def build_render_backend(self):
        """Instantiate the configured RenderBackend."""
        if self.render_backend == "self_host":
            from .render.self_host import SelfHostRenderBackend

            return SelfHostRenderBackend()
        from .render.cloud import CloudRenderBackend

        return CloudRenderBackend()
