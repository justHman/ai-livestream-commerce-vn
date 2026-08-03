"""Offline-safe defaults for collecting/importing core.server in tests.

The default RENDER_BACKEND=cloud makes core.server's module-level
``app = create_app()`` construct CloudRenderBackend, which raises
LiveAvatarError at import time when LIVEAVATAR_API_KEY is absent. These
defaults make collection safe in a clean env. Tests that actually need the
cloud backend must set RENDER_BACKEND=cloud + LIVEAVATAR_API_KEY
themselves (the smoke tests skip when the key is missing).
"""

import os

# setdefault preserves explicit user env / CLI overrides.
os.environ.setdefault("RENDER_BACKEND", "mock")
os.environ.setdefault("LLM_ENGINE", "none")
os.environ.setdefault("TTS_ENGINE", "tone")
os.environ.setdefault("DIRECTOR_ENABLED", "0")
os.environ.setdefault("DIRECTOR_EMBEDDER", "hash")
os.environ.setdefault("APP_ENV", "dev")
