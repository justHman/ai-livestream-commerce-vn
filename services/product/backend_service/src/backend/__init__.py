"""Canonical backend service package with staged legacy compatibility."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        return import_module("core.server").app
    if name == "create_app":
        return import_module("core.server").create_app
    raise AttributeError(name)
