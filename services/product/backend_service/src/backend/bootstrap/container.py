"""backend.bootstrap.container — typed resource references, no business logic.

``BootstrapContainer`` is a lightweight holder of constructed resource references.
It is NOT a DI framework, a service locator, or a runtime registry. Containers
are built by ``create_container()`` and attached to ``app.state`` by the factory.

Access pattern (Task 1.16):
    REST:  request.app.state.container
    WS:    websocket.app.state.container  (before accept)
    Test:  BootstrapContainer(...) via create_app(container=...)

Each ``create_app()`` call gets a FRESH container. Two apps with different
containers never share state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.api.v1 import ControlHub
    from core.config import AppConfig
    from core.director.coordinator import DirectorCoordinator
    from core.director.runtime import DirectorRuntime
    from core.engine_manager import EngineManager
    from core.livekit_publish import LiveKitPublisherRegistry
    from core.render.base import RenderBackend
    from core.render.locks import SessionLockRegistry
    from core.store import SessionStore


@dataclass
class BootstrapContainer:
    """Typed resource references for the backend service.

    Container is constructed by ``create_container()`` or by tests.  It holds
    only references — no business methods, no runtime swapping logic, no
    mutation of global state.

    Fields are typed as optional because some resources are not available
    in all configurations (e.g. no Postgres when DATABASE_URL is unset).
    """

    # -- Core resources --
    backend: RenderBackend
    store: SessionStore
    config: AppConfig

    # -- Optional engine / director --
    engine_manager: EngineManager | None = None
    director: DirectorRuntime | None = None
    coordinator: DirectorCoordinator | None = None

    # -- Optional infrastructure --
    pg_store: Any = None  # PostgresRuntimeStore or None
    livekit_publishers: LiveKitPublisherRegistry | None = None

    # -- Per-session state (owned by this container, not global) --
    hub: ControlHub | None = None
    locks: SessionLockRegistry | None = None
    orchestrators: dict = field(default_factory=dict)
    # Legacy avatars store — moved to container scope
    avatars: Any = None  # AvatarStore or None


def create_container(
    *,
    backend: RenderBackend,
    store: SessionStore,
    config: AppConfig,
    engine_manager: EngineManager | None = None,
    director: DirectorRuntime | None = None,
    coordinator: DirectorCoordinator | None = None,
    pg_store: Any = None,
    livekit_publishers: LiveKitPublisherRegistry | None = None,
    hub: ControlHub | None = None,
    locks: SessionLockRegistry | None = None,
    avatars: Any = None,
) -> BootstrapContainer:
    """Construct a BootstrapContainer with the given resources.

    Callers (``build_lifespan`` and tests) pass already-constructed
    resources.  This function is a convenience factory, not a DI engine.
    """
    from core.api.v1 import AvatarStore, ControlHub
    from core.render.locks import SessionLockRegistry

    return BootstrapContainer(
        backend=backend,
        store=store,
        config=config,
        engine_manager=engine_manager,
        director=director,
        coordinator=coordinator,
        pg_store=pg_store,
        livekit_publishers=livekit_publishers,
        hub=hub or ControlHub(),
        locks=locks or SessionLockRegistry(),
        avatars=avatars or AvatarStore(),
    )
