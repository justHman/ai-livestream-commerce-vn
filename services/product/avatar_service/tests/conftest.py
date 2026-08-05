"""Test configuration: ensure the avatar service package is importable.

Per-test env defaults are applied by the ``offline_env`` fixture (OpenSpec
1.51) — no collection-time os.environ mutation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default self-host service env: no model loads, auth off (per test)."""
    monkeypatch.setenv("AVATAR_ENGINE", "none")
    monkeypatch.setenv("AVATAR_AUTH_ENABLED", "0")
    monkeypatch.setenv("LIVEKIT_URL", "ws://localhost:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key-test-key-test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-secret-test-secret-test-secret")
