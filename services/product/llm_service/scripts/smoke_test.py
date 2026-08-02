"""Smoke-test the canonical LLM package import."""

from llm import LLMEngine

assert LLMEngine.__name__ == "LLMEngine"
print("llm import: ok")
