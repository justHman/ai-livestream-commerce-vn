"""Tasks 7.4/7.9 tests: no-LLM deterministic preview + calibration boundary.

These tests prove:

- ``preview_product``/``preview_batch`` make ZERO LLM calls (pure
  arithmetic) and are deterministic for identical calibration/targets.
- ``calibration.py``/``preview.py`` never import the Change A
  ``SpeechDurationEstimator`` or anything from ``text_chunker`` (task 7.9):
  pre-generation ``GenerationBudgetCalibration`` is a distinct concern from
  post-generation speech-duration estimation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.application.script_authoring.generation.calibration import (
    GenerationBudgetCalibration,
    GenerationBudgetError,
)
from backend.application.script_authoring.generation.preview import (
    PLANNING_CALLS_PER_PRODUCT,
    preview_batch,
    preview_product,
)

DEFAULT = GenerationBudgetCalibration()
"""Default conservative calibration (values frozen by the pydantic model)."""

_GENERATION_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "backend"
    / "application"
    / "script_authoring"
    / "generation"
)


def _module_sources() -> str:
    """Concatenated source of the calibration and preview modules."""
    parts: list[str] = []
    for name in ("calibration.py", "preview.py"):
        parts.append((_GENERATION_DIR / name).read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_preview_makes_zero_llm_calls() -> None:
    """Preview is pure arithmetic: no LLM client module is imported."""
    preview_product("P001", 600.0, DEFAULT)
    preview_batch([("P001", 600.0), ("P002", 3600.0)], DEFAULT)
    source = _module_sources()
    assert "llm_engines" not in source
    assert "LLMEngine" not in source
    assert "generate(" not in source


def test_preview_is_deterministic() -> None:
    """Identical calibration/targets produce identical previews."""
    assert preview_product("P001", 1800.0, DEFAULT) == preview_product("P001", 1800.0, DEFAULT)
    assert preview_batch([("P001", 1800.0)], DEFAULT) == preview_batch([("P001", 1800.0)], DEFAULT)


def test_k_formula_default_calibration() -> None:
    """K = ceil(target / safe_segment_duration), clamped to K bounds."""
    # safe_output_tokens = floor(4096 * 0.5) = 2048
    # safe_segment_duration = 2048 / 8.0 = 256 s
    assert DEFAULT.safe_output_tokens() == 2048
    assert DEFAULT.safe_segment_duration_s() == 256.0
    # 600 s -> ceil(600 / 256) = 3; calls = 1 + 3 = 4 (Decision 11 example).
    preview = preview_product("P001", 600.0, DEFAULT)
    assert preview.planned_segment_count == 3
    assert preview.estimated_semantic_calls == 4
    # 3600 s -> ceil(3600 / 256) = 15; calls = 1 + 15 = 16 (Decision 11).
    preview = preview_product("P002", 3600.0, DEFAULT)
    assert preview.planned_segment_count == 15
    assert preview.estimated_semantic_calls == 16


def test_preview_maximum_semantic_calls_is_backend_owned_bound() -> None:
    """Reviewer R9.2: preview distinguishes PLANNED (1+K) from MAXIMUM
    Generate calls (1+K*segment_max_attempts) — the backend-owned bound for
    bounded in-place Segment Repair. The model never controls it."""
    preview = preview_product("P001", 600.0, DEFAULT, segment_max_attempts=3)
    assert preview.planned_segment_count == 3
    assert preview.estimated_semantic_calls == 4  # 1 + K (planned)
    assert preview.maximum_semantic_calls == 1 + 3 * 3  # 1 + K*N = 10


def test_k_respects_segment_count_bounds() -> None:
    """K clamps to max_segment_count even when ceil exceeds it."""
    wide = GenerationBudgetCalibration(max_segment_count=5)
    preview = preview_product("P001", 3600.0, wide)
    assert preview.planned_segment_count == 5
    assert preview.estimated_semantic_calls == 6


def test_batch_preview_aggregates_total() -> None:
    """Batch sums per-product estimates in deterministic input order."""
    batch = preview_batch(
        [("P001", 600.0), ("P002", 3600.0), ("P003", 1200.0)],
        DEFAULT,
    )
    assert [p.product_id for p in batch.products] == ["P001", "P002", "P003"]
    assert batch.estimated_semantic_calls_total == (4 + 16 + 6)
    assert PLANNING_CALLS_PER_PRODUCT == 1


def test_out_of_bounds_target_rejected() -> None:
    """Targets outside the configured limits fail deterministically."""
    for bad in (-1.0, 0.0, 100.0, 99999.0, float("nan"), float("inf")):
        with pytest.raises(GenerationBudgetError):
            preview_product("P001", bad, DEFAULT)


def test_calibration_configuration_validation() -> None:
    """Calibration model rejects impossible limits."""
    with pytest.raises(ValueError):
        GenerationBudgetCalibration(min_target_duration_s=3600.0, max_target_duration_s=600.0)
    with pytest.raises(ValueError):
        GenerationBudgetCalibration(min_segment_count=10, max_segment_count=2)
    with pytest.raises(ValueError):
        GenerationBudgetCalibration(output_safety_factor=0.0)
    with pytest.raises(ValueError):
        GenerationBudgetCalibration(model_max_output_tokens=0)


def test_calibration_never_imports_speech_duration_estimator() -> None:
    """Task 7.9: calibration/preview never touch Change A duration logic."""
    source = _module_sources()
    assert "SpeechDurationEstimator" not in source
    assert "text_chunker" not in source
    assert "speech_chunking" not in source
