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
    from backend.application.render.engines_base import RenderBackend
    from backend.application.render.locks import SessionLockRegistry
    from backend.application.publishing import LiveKitPublisherRegistry
    from backend.api.v1.hub import ControlHub
    from backend.application.db import SessionStore
    from backend.application.director.coordinator import DirectorCoordinator
    from backend.application.director.session_context import DirectorRuntime
    from backend.config import AppConfig
    from backend.engine_manager import EngineManager


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

    # -- Change B script authoring (approved-script-authoring-pipeline) --
    # Container-scoped authoring capability consumed by ``api/v1/scripts``
    # and the session binding endpoint (task 12.2). When None, the
    # /script-sets and session binding surfaces return 501.
    script_authoring_service: Any = None

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
    script_authoring_service: Any = None,
) -> BootstrapContainer:
    """Construct a BootstrapContainer with the given resources.

    Callers (``build_lifespan`` and tests) pass already-constructed
    resources.  This function is a convenience factory, not a DI engine.
    """
    from backend.application.render.locks import SessionLockRegistry
    from backend.api.v1.hub import AvatarStore, ControlHub

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
        script_authoring_service=script_authoring_service,
    )
