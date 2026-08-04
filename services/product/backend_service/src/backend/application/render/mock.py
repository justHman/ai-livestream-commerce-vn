"""MockRenderBackend — shared identity with the avatar_service mock.

The avatar_service owns the mock renderer (media plane). The backend
re-exports it so isinstance checks and the ENGINES/registry parity keep one
class identity across core shims and canonical services.
"""

from __future__ import annotations

from avatar.engines.mock import (  # noqa: F401
    MockRenderBackend,
)
