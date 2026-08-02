"""Smoke-test the canonical backend package import."""

import backend

assert backend.__name__ == "backend"
print("backend import: ok")
