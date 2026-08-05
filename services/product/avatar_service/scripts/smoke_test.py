"""Smoke-test the canonical avatar package import."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from avatar import RenderBackend  # noqa: E402,I001

assert RenderBackend.__name__ == "RenderBackend"
print("avatar import: ok")
