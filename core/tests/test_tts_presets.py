"""Tests for Phase A TTS preset registry (6-preset selector).

Covers:
  - AVAILABLE_TTS_PRESETS exposes exactly the 6 ids from the Phase A spec
  - Each preset entry has the required keys (id/label/engine/weights_path/
    sample_rate/device/notes)
  - vieneu-v2 -> 24 kHz, vieneu-v3-turbo -> 48 kHz (variant detection)
  - get_tts_preset returns a copy matching id; unknown id -> None
  - EngineManager.apply_tts_preset(id) updates tts_cfg with the preset's
    engine/weights_path/sample_rate/device without loading any model
  - apply_tts_preset on unknown id raises KeyError
  - TTSConfig.from_env: when TTS_PRESET_ID is set, the preset's engine/
    weights/sample_rate WIN over individual TTS_* fields
  - TTSConfig.from_env: when TTS_PRESET_ID is NOT set, defaults stay on the
    offline-safe ``transformers``/empty/24 kHz combo so the test baseline
    keeps booting without vieneu

All tests are offline; no model downloads, no GPU.
"""

from __future__ import annotations


import pytest

from core.config import TTSConfig
from core.engine_manager import (
    AVAILABLE_TTS_PRESETS,
    EngineManager,
    get_tts_preset,
)


# ---------- registry shape ----------


EXPECTED_IDS = [
    "vieneu-v3-turbo",
    "vieneu-v2",
    "cosyvoice2",
    "kokoro",
    "xtts-v2",
    "transformers-mms-vi",
]

REQUIRED_KEYS = {"id", "label", "engine", "weights_path", "sample_rate", "device", "notes"}


def test_available_tts_presets_has_all_six_ids():
    actual_ids = [p["id"] for p in AVAILABLE_TTS_PRESETS]
    for expected in EXPECTED_IDS:
        assert expected in actual_ids, f"missing preset id: {expected}"
    assert len(AVAILABLE_TTS_PRESETS) == 6


def test_each_preset_has_required_keys():
    for preset in AVAILABLE_TTS_PRESETS:
        missing = REQUIRED_KEYS - set(preset.keys())
        assert not missing, f"preset {preset.get('id')} missing keys: {missing}"
        assert isinstance(preset["sample_rate"], int)
        assert preset["label"], "label must be non-empty"
        assert preset["weights_path"], "weights_path must be non-empty"


def test_vieneu_v2_sample_rate_24k():
    preset = get_tts_preset("vieneu-v2")
    assert preset is not None
    assert preset["sample_rate"] == 24000
    assert preset["weights_path"] == "pnnbao-ump/VieNeu-TTS-v2"
    assert preset["engine"] == "vieneu"


def test_vieneu_v3_turbo_sample_rate_48k():
    preset = get_tts_preset("vieneu-v3-turbo")
    assert preset is not None
    assert preset["sample_rate"] == 48000
    assert preset["weights_path"] == "pnnbao-ump/VieNeu-TTS-v3-Turbo"
    assert preset["engine"] == "vieneu"


def test_get_tts_preset_unknown_returns_none():
    assert get_tts_preset("not-a-real-preset") is None


def test_other_preset_engine_mappings():
    """Spot-check the non-VieNeu presets map to their documented engines."""
    cases = {
        "cosyvoice2": ("cosyvoice", "FunAudioLLM/CosyVoice2-0.5B", 24000),
        "kokoro": ("kokoro", "hexgrad/Kokoro-82M", 24000),
        "xtts-v2": ("xtts", "coqui/XTTS-v2", 24000),
        "transformers-mms-vi": ("transformers", "facebook/mms-tts-vie", 16000),
    }
    for preset_id, (engine, weights, sr) in cases.items():
        preset = get_tts_preset(preset_id)
        assert preset is not None, preset_id
        assert preset["engine"] == engine
        assert preset["weights_path"] == weights
        assert preset["sample_rate"] == sr


# ---------- EngineManager.apply_tts_preset ----------


def test_apply_tts_preset_updates_manager_cfg_for_v3_turbo():
    mgr = EngineManager()
    cfg = mgr.apply_tts_preset("vieneu-v3-turbo")
    assert cfg["engine"] == "vieneu"
    assert cfg["weights_path"] == "pnnbao-ump/VieNeu-TTS-v3-Turbo"
    assert cfg["sample_rate"] == 48000
    # Manager state reflects the same cfg.
    assert mgr.tts_cfg["engine"] == "vieneu"
    assert mgr.tts_cfg["sample_rate"] == 48000
    assert mgr.tts_cfg["weights_path"] == "pnnbao-ump/VieNeu-TTS-v3-Turbo"


def test_apply_tts_preset_updates_manager_cfg_for_v2():
    mgr = EngineManager()
    mgr.apply_tts_preset("vieneu-v2")
    assert mgr.tts_cfg["sample_rate"] == 24000
    assert mgr.tts_cfg["weights_path"] == "pnnbao-ump/VieNeu-TTS-v2"


def test_apply_tts_preset_switches_between_engines():
    """Switching from cosyvoice2 -> transformers-mms-vi rewrites engine + sr."""
    mgr = EngineManager()
    mgr.apply_tts_preset("cosyvoice2")
    assert mgr.tts_cfg["engine"] == "cosyvoice"
    mgr.apply_tts_preset("transformers-mms-vi")
    assert mgr.tts_cfg["engine"] == "transformers"
    assert mgr.tts_cfg["sample_rate"] == 16000


def test_apply_tts_preset_unknown_raises():
    mgr = EngineManager()
    with pytest.raises(KeyError):
        mgr.apply_tts_preset("does-not-exist")


def test_apply_tts_preset_does_not_load_engine():
    """Applying a preset is metadata-only; the engine stays None."""
    mgr = EngineManager()
    mgr.apply_tts_preset("vieneu-v3-turbo")
    assert mgr.tts is None  # no model load attempted


# ---------- TTSConfig.from_env: preset wins / preserves offline default ----


def test_tts_config_default_is_offline_safe(monkeypatch):
    """No TTS_* env -> engine stays on 'transformers'/empty so pytest stays offline."""
    for key in (
        "TTS_PRESET_ID",
        "TTS_ENGINE",
        "TTS_MODEL",
        "TTS_WEIGHTS",
        "TTS_SAMPLE_RATE",
        "TTS_DEVICE",
        "TTS_REF_AUDIO",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = TTSConfig.from_env()
    assert cfg.engine == "transformers"
    assert cfg.model == ""
    assert cfg.sample_rate == 24000
    # The dropdown default is the spec's recommended preset.
    assert cfg.preset_id == "vieneu-v3-turbo"


def test_tts_config_preset_id_wins_over_individual_fields(monkeypatch):
    """TTS_PRESET_ID set -> preset's engine/weights/sample_rate override env."""
    monkeypatch.setenv("TTS_PRESET_ID", "vieneu-v3-turbo")
    monkeypatch.setenv("TTS_ENGINE", "transformers")
    monkeypatch.setenv("TTS_WEIGHTS", "facebook/mms-tts-vie")
    monkeypatch.setenv("TTS_SAMPLE_RATE", "16000")
    cfg = TTSConfig.from_env()
    assert cfg.engine == "vieneu"
    assert cfg.model == "pnnbao-ump/VieNeu-TTS-v3-Turbo"
    assert cfg.sample_rate == 48000
    assert cfg.preset_id == "vieneu-v3-turbo"


def test_tts_config_preset_id_v2(monkeypatch):
    monkeypatch.setenv("TTS_PRESET_ID", "vieneu-v2")
    cfg = TTSConfig.from_env()
    assert cfg.engine == "vieneu"
    assert cfg.model == "pnnbao-ump/VieNeu-TTS-v2"
    assert cfg.sample_rate == 24000


def test_tts_config_unknown_preset_id_falls_back_to_env(monkeypatch):
    """An unknown TTS_PRESET_ID leaves the individual env values in charge."""
    monkeypatch.setenv("TTS_PRESET_ID", "bogus-preset")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("TTS_SAMPLE_RATE", "22050")
    cfg = TTSConfig.from_env()
    assert cfg.engine == "tone"
    assert cfg.sample_rate == 22050
    assert cfg.preset_id == "bogus-preset"
