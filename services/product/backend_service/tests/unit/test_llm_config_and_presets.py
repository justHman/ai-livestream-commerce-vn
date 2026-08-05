"""Offline tests for backend LLMConfig.stream env parsing + Qwen3.5 preset.

Migrated from core/tests/test_llm_streaming.py (moved here in fix round
1.50-1.59: ``LLMConfig`` and ``AVAILABLE_LLM_PRESETS`` live in the backend
control plane, not the llm_service — the llm_service rejects hosted adapters
and has no LLMConfig).
"""

from __future__ import annotations

import pytest

from backend.config import LLMConfig
from backend.engine_manager import AVAILABLE_LLM_PRESETS


# ---------- LLMConfig.stream env parsing ----------


def test_llm_config_stream_default_false_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_STREAM", raising=False)

    cfg = LLMConfig.from_env()

    assert cfg.stream is False


@pytest.mark.parametrize("val", ["1", "true", "on", "TRUE", "On", "YES"])
def test_llm_config_stream_truthy_values(monkeypatch, val):
    monkeypatch.setenv("LLM_STREAM", val)

    cfg = LLMConfig.from_env()

    assert cfg.stream is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no", "", "random"])
def test_llm_config_stream_non_truthy_values(monkeypatch, val):
    monkeypatch.setenv("LLM_STREAM", val)

    cfg = LLMConfig.from_env()

    assert cfg.stream is False


def test_llm_config_stream_propagated_to_engine_cfg(monkeypatch):
    monkeypatch.setenv("LLM_STREAM", "1")

    cfg = LLMConfig.from_env()
    out = cfg.to_engine_cfg()

    assert out.get("stream") is True


# ---------- engine_manager Qwen3.5 preset ----------


def test_qwen35_preset_present_by_label():
    labels = [p.get("label", "") for p in AVAILABLE_LLM_PRESETS]

    assert any("Qwen3.5" in lab for lab in labels), labels


def test_qwen35_preset_exact_fields():
    qwen35 = next(p for p in AVAILABLE_LLM_PRESETS if p.get("label", "").startswith("Qwen3.5"))

    assert qwen35["engine"] == "llamacpp"
    assert qwen35["model"] == "unsloth/Qwen3.5-4B-GGUF"
    assert qwen35["gguf_file"] == "Qwen3.5-4B-Q4_K_M.gguf"
    assert qwen35["device"] == "cuda"
    assert qwen35["n_gpu_layers"] == -1
