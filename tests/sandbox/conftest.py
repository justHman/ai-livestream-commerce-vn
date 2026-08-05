"""Sandbox test configuration (OpenSpec 1.50).

Sandbox tests are NOT selected by ordinary CI (root testpaths excludes
``tests/sandbox``). When explicitly selected, missing credentials fail
loudly rather than silently skipping — the guard below is collection-time
and module-scoped so any sandbox module import without credentials aborts.
"""

from __future__ import annotations

import os

_REQUIRED = {
    "LIVEAVATAR_API_KEY": "LiveAvatar sandbox credential",
}


def _missing() -> list[str]:
    return [name for name in _REQUIRED if not os.environ.get(name)]


def pytest_configure(config) -> None:
    missing = _missing()
    if missing:
        raise RuntimeError(
            "sandbox tests require: "
            + ", ".join(f"{name} ({label})" for name, label in _REQUIRED.items())
            + " — set them explicitly; sandbox tests never silently skip"
        )
