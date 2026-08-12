"""Test configuration: ensure the TTS service package is importable.

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
    """Default self-host service env: no model loads, auth off (per test).

    ``TTS_PROVIDER=none`` keeps the lifespan from building the real VieNeu
    provider (which downloads weights on import) — provider tests opt in by
    setting ``TTS_PROVIDER=vieneu_v3`` themselves.
    """
    monkeypatch.setenv("TTS_ENGINE", "none")
    monkeypatch.setenv("TTS_PROVIDER", "none")
    monkeypatch.setenv("TTS_AUTH_ENABLED", "0")
