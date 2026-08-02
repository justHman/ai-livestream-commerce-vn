"""Smoke-test the canonical LLM package import."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm import LLMEngine  # noqa: E402,I001

assert LLMEngine.__name__ == "LLMEngine"
print("llm import: ok")
