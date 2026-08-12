"""Tasks 4.2a/4.2b tests: spoken-duration parity with Change A.

``script_authoring.duration.spoken_duration_ms`` MUST be a thin delegation
to the canonical Change A ``SpeechDurationEstimator`` exported from the
``backend.application.text_chunker`` package root — never a deep-import of
``text_chunker.duration`` and never a duplicate estimator.
"""

from __future__ import annotations

import inspect
import importlib

import pytest

from backend.application.script_authoring import duration as authoring_duration
from backend.application.text_chunker import SpeechDurationEstimator

# Vietnamese sample table: plain prose, prices, percents, acronyms, mixed.
_VI_SAMPLES = [
    "Xin chào các bạn, hôm nay mình giới thiệu kem dưỡng ẩm.",
    "Kem ABC chỉ 299.000đ, giảm 20% hôm nay.",
    "Số lượng có hạn, giá 99đ mỗi cái.",
    "Mã SKU-123 hàng chính hãng, bảo hành 12 tháng.",
    "Đừng bỏ lỡ cơ hội này, đặt hàng ngay hôm nay.",
    "Tinh chất serum dưỡng da với vitamin C và E.",
    "",
    "50% khuyến mãi chỉ trong 3 ngày.",
    "Sản phẩm TTS-2000 mới nhất 2026.",
]


def test_spoken_duration_matches_upstream_estimator() -> None:
    """Parity: authoring duration equals the Change A estimator exactly."""
    estimator = SpeechDurationEstimator()
    for sample in _VI_SAMPLES:
        assert authoring_duration.spoken_duration_ms(sample) == pytest.approx(
            estimator.estimate_ms(sample)
        )


def test_parity_sanity_sentence() -> None:
    assert authoring_duration.spoken_duration_ms("Kem ABC chỉ 299.000đ") == (
        SpeechDurationEstimator().estimate_ms("Kem ABC chỉ 299.000đ")
    )


def test_empty_text_is_zero() -> None:
    assert authoring_duration.spoken_duration_ms("") == 0.0
    assert authoring_duration.spoken_duration_ms("") == (SpeechDurationEstimator().estimate_ms(""))


def test_duration_module_imports_estimator_from_package_root() -> None:
    """The delegation seam must use the package root, never a deep-import."""
    module = importlib.import_module("backend.application.script_authoring.duration")
    source = inspect.getsource(module)
    assert "from backend.application.text_chunker import SpeechDurationEstimator" in source
    assert "from backend.application.text_chunker.duration" not in source


def test_duration_module_contains_no_duplicate_algorithm() -> None:
    """No second syllable/coefficient estimator may exist in authoring."""
    source = inspect.getsource(authoring_duration)
    for forbidden in ("_CURRENCY", "syllable_ms", "estimate_syllables", "DurationCoefficients"):
        assert forbidden not in source, f"duplicate estimator artifact {forbidden!r} found"
