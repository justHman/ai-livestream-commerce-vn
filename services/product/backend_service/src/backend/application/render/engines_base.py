"""Render backend type contracts — shared identity with the avatar_service.

The avatar_service owns the render-backend seam (media plane). The backend
control plane re-exports the canonical types so isinstance checks against
``StreamingAvatarBackend``/``FullPipelineBackend`` keep working across the
legacy core shims and the canonical services (one class identity). core/
stays untouched; avatar_service is the canonical engine owner. The 1.50-1.59
HTTP-client refactor will replace in-process render orchestration.
"""

from __future__ import annotations

from avatar.engines.base import (  # noqa: F401
    AvatarEngine,
    EngineError,
    EngineUnavailable,
    FullPipelineBackend,
    RenderBackend,
    StartOptions,
    StartResult,
    StreamingAvatarBackend,
)
from avatar.engines.windows import (  # noqa: F401
    AudioWindow,
    TextChunk,
    VideoWindow,
    num_frames_for,
    split_waveform,
)
