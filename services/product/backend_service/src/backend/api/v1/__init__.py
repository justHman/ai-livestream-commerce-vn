"""backend.api.v1 — canonical control-plane shared state (OpenSpec 1.21).

The full versioned route set remains the legacy ``core.api.v1`` surface
until Task 1.26; the canonical backend service exposes only the shared
state objects the typed container wires (hub + avatar store).
"""

from .hub import AvatarStore, ControlHub

__all__ = ["ControlHub", "AvatarStore"]
