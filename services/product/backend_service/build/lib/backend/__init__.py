"""Canonical backend service package with an explicit staged compatibility seam."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        return import_module("backend.main").app
    if name == "create_app":
        return import_module("backend.bootstrap").create_app
    raise AttributeError(name)
