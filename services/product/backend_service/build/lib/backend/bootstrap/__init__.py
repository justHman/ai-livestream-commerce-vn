"""backend.bootstrap — composition root package.

Package layout:
    __init__.py     Public exports (create_app, BootstrapContainer, create_container).
    app_factory.py  FastAPI construction with middleware, lifespan, and router wiring.
    container.py    Typed ``BootstrapContainer`` — lightweight resource references.
    lifespan.py     Bounded startup/shutdown lifecycle for all resources.
"""

from .app_factory import create_app
from .container import BootstrapContainer, create_container
from .lifespan import build_lifespan

__all__ = ["BootstrapContainer", "build_lifespan", "create_app", "create_container"]
