"""Smoke-test the actual canonical backend ASGI entrypoint."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
# Canonical sibling service packages (llm/tts/avatar) — same PYTHONPATH
# layout the backend Dockerfile uses for the self-contained seam.
sys.path[:0] = [
    str(ROOT / "services/product/backend_service/src"),
    str(ROOT / "services/product/llm_service/src"),
    str(ROOT / "services/product/tts_service/src"),
    str(ROOT / "services/product/avatar_service/src"),
    str(ROOT),
]
os.environ.update(
    APP_ENV="dev",
    DIRECTOR_ENABLED="0",
    LLM_ENGINE="none",
    RENDER_BACKEND="mock",
    SESSION_STORE="memory",
    TTS_ENGINE="tone",
)

from backend.main import app  # noqa: E402,I001

# ponytail: use a service-local app after Tasks 1.11–1.24 remove the compatibility seam.

assert app is not None
print("backend app import: ok")
