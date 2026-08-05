"""Offline tests for the Pipecat config gate.

The canonical backend has no pipecat bridge module (config-only toggle until
the feature is wired). The gate is ``AppConfig.pipecat_enabled`` parsed from
``PIPECAT_ENABLED`` — the same env the legacy bridge stub read.
"""

from __future__ import annotations

import pytest

from backend.config import AppConfig


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PIPECAT_ENABLED", raising=False)
    assert AppConfig.from_env().pipecat_enabled is False


def test_enabled_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PIPECAT_ENABLED", "1")
    assert AppConfig.from_env().pipecat_enabled is True
    monkeypatch.setenv("PIPECAT_ENABLED", "true")
    assert AppConfig.from_env().pipecat_enabled is True


def test_legacy_truthy_values_accepted(monkeypatch: pytest.MonkeyPatch):
    """The legacy bridge stub treated on/yes as truthy; the canonical gate
    preserves that set so existing deployments behave identically."""
    for value in ("on", "yes", "ON", "Yes"):
        monkeypatch.setenv("PIPECAT_ENABLED", value)
        assert AppConfig.from_env().pipecat_enabled is True


def test_build_pipeline_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PIPECAT_ENABLED", "0")
    cfg = AppConfig.from_env()
    # No bridge exists in the canonical backend; the gate being off means
    # the caller keeps using StreamOrchestrator (no pipeline to build).
    assert cfg.pipecat_enabled is False
