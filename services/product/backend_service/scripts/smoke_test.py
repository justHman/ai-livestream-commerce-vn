"""Smoke-test the actual canonical backend ASGI entrypoint."""

from services.product.backend_service.src.backend.main import app

assert app is not None
print("backend app import: ok")
