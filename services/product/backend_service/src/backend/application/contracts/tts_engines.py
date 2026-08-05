"""TTS engine type contracts — shared registry with the tts_service.

Until the 1.50-1.59 HTTP-client refactor replaces in-process engine loading,
the backend re-exports the canonical tts_service engine seam so the ENGINES
registry is shared (tests and the EngineManager register/load into the same
registry). core/ stays untouched; tts_service is the canonical engine owner.
"""

from __future__ import annotations

from tts.engines.base import (  # noqa: F401
    AudioChunk,
    ENGINES,
    TTSEngine,
    TTSRequest,
    EngineError,
    EngineUnavailable,
    load_engine,
    register_engine,
    to_tts_fn,
)
from tts.engines.base import ToneEngine  # noqa: F401
