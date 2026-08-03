"""Test configuration: ensure the TTS service package is importable."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("TTS_ENGINE", "none")
os.environ.setdefault("TTS_AUTH_ENABLED", "0")