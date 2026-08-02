"""Smoke-test the canonical avatar package import."""

from avatar import RenderBackend

assert RenderBackend.__name__ == "RenderBackend"
print("avatar import: ok")
