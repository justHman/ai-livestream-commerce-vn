"""Offline tests for Pipecat bridge stub."""

from __future__ import annotations

import pytest

from core import pipecat_bridge


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PIPECAT_ENABLED", raising=False)
    assert pipecat_bridge.is_enabled() is False


def test_enabled_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PIPECAT_ENABLED", "1")
    assert pipecat_bridge.is_enabled() is True
    monkeypatch.setenv("PIPECAT_ENABLED", "true")
    assert pipecat_bridge.is_enabled() is True


def test_build_pipeline_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PIPECAT_ENABLED", "0")
    assert pipecat_bridge.build_pipeline() is None


def test_build_pipeline_raises_when_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PIPECAT_ENABLED", "1")
    with pytest.raises(NotImplementedError, match="install pipecat"):
        pipecat_bridge.build_pipeline()


@pytest.mark.asyncio
async def test_run_turn_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PIPECAT_ENABLED", "0")
    assert await pipecat_bridge.run_turn(session_id="s1") is None


@pytest.mark.asyncio
async def test_run_turn_raises_when_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PIPECAT_ENABLED", "1")
    with pytest.raises(NotImplementedError, match="install pipecat"):
        await pipecat_bridge.run_turn(session_id="s1")


def test_is_enabled_env_override_dict():
    assert pipecat_bridge.is_enabled({"PIPECAT_ENABLED": "yes"}) is True
    assert pipecat_bridge.is_enabled({"PIPECAT_ENABLED": "0"}) is False
