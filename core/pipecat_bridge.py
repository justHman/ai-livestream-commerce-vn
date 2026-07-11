"""Pipecat pipeline bridge (Wave C stub).

Default OFF (PIPECAT_ENABLED=0). When disabled, callers keep using
StreamOrchestrator. When enabled, build_pipeline raises until full wire-up.
"""

from __future__ import annotations

import os
from typing import Any, Optional


def is_enabled(env: Optional[dict[str, str]] = None) -> bool:
    """Return True when PIPECAT_ENABLED is truthy."""
    source = env if env is not None else os.environ
    return str(source.get("PIPECAT_ENABLED", "0")).lower() in (
        "1",
        "true",
        "on",
        "yes",
    )


def build_pipeline(*args: Any, **kwargs: Any) -> Any:
    """Build a Pipecat pipeline.

    Raises NotImplementedError when the feature flag is on (Wave C full).
    Returns None when disabled so callers fall back to StreamOrchestrator.
    """
    if not is_enabled():
        return None
    raise NotImplementedError("install pipecat; wire in Wave C full")


async def run_turn(*args: Any, **kwargs: Any) -> Optional[Any]:
    """Run one dialogue turn through Pipecat when enabled.

    Disabled path returns None (caller uses StreamOrchestrator).
    Enabled path attempts build_pipeline then fails until Wave C.
    """
    pipeline = build_pipeline(*args, **kwargs)
    if pipeline is None:
        return None
    # Unreachable until build_pipeline is implemented.
    raise NotImplementedError("install pipecat; wire in Wave C full")
