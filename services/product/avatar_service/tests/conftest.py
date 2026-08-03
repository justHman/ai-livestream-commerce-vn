"""Test configuration: ensure the avatar service package is importable."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("AVATAR_ENGINE", "none")
os.environ.setdefault("AVATAR_AUTH_ENABLED", "0")
os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "test-key-test-key-test-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-secret-test-secret-test-secret")