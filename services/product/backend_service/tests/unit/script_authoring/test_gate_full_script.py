"""Full Script Gate rule tests (task 3.10).

Cross-segment repetition, contradictory claims, required coverage, CTA
pacing, tone consistency, transition policy, and total spoken duration —
with violations attributed to the implicated segment index.
"""

from __future__ import annotations

from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.rules.full_script import (
    check_contradictory_claims,
    check_cross_segment_repetition,
    check_cta_pacing,
    check_required_coverage,
    check_tone_consistency,
    check_total_duration,
    check_transition_policy,
)

_FACTS = ProductFacts(prices=("299.000",), skus=("SKU-P004",))


def _ctx(**overrides) -> ScriptGateContext:
    return ScriptGateContext(facts=_FACTS, **overrides)


def test_cross_segment_repetition_flags_repeated_phrase() -> None:
    segments = [
        "Kem ABC dưỡng ẩm sâu giá 299.000đ.",
        "Kem ABC dưỡng ẩm sâu thêm lần nữa.",
    ]
    violations = check_cross_segment_repetition(segments, _ctx())
    assert any(v.rule_id == "REPETITION_CROSS" for v in violations)
    assert all(v.segment_index in (0, 1) for v in violations)


def test_cross_segment_repetition_clean() -> None:
    segments = [
        "Kem ABC dưỡng ẩm sâu giá 299.000đ.",
        "Gel rửa mặt làm sạch da hiệu quả.",
    ]
    assert check_cross_segment_repetition(segments, _ctx()) == []


def test_contradictory_claims_flagged() -> None:
    segments = ["Sản phẩm này an toàn cho da.", "Sản phẩm này độc hại, đừng mua."]
    violations = check_contradictory_claims(segments, _ctx())
    assert any(v.rule_id == "CLAIM_CONTRADICTION" for v in violations)
    assert violations[0].segment_index == 1


def test_required_coverage_missing() -> None:
    segments = ["Kem ABC dưỡng ẩm sâu."]
    ctx = _ctx(required_topics=("cách dùng",))
    violations = check_required_coverage(segments, ctx)
    assert any(v.rule_id == "COVERAGE_REQUIRED" for v in violations)


def test_required_coverage_present() -> None:
    segments = ["Cách dùng: thoa mỗi tối trước khi ngủ."]
    ctx = _ctx(required_topics=("cách dùng",))
    assert check_required_coverage(segments, ctx) == []


def test_cta_pacing_exceeds_script_limit() -> None:
    segments = ["Mua ngay! Đặt ngay!", "Chốt đơn! Order ngay!"]
    ctx = _ctx(max_cta_per_segment=1)
    violations = check_cta_pacing(segments, ctx)
    assert any(v.rule_id == "CTA_PACING" for v in violations)


def test_tone_consistency_flags_shouting() -> None:
    segments = ["MUA NGAY KẺO HẾT HÀNG!", "ĐẶT HÀNG NGAY HÔM NAY!"]
    violations = check_tone_consistency(segments, _ctx())
    assert any(v.rule_id == "TONE_CONSISTENCY" for v in violations)


def test_transition_policy_order_agnostic_forbids_other_product() -> None:
    ctx = _ctx(transition_policy="ORDER_AGNOSTIC", other_product_names=("Gel rửa mặt",))
    violations = check_transition_policy(["Sau gel rửa mặt thì dùng kem ABC."], ctx)
    assert any(v.rule_id == "TRANSITION_ORDER" for v in violations)


def test_transition_policy_order_aware_allows_mention() -> None:
    ctx = _ctx(transition_policy="ORDER_AWARE", other_product_names=("Gel rửa mặt",))
    assert check_transition_policy(["Sau gel rửa mặt thì dùng kem ABC."], ctx) == []


def test_total_duration_out_of_range() -> None:
    segments = ["Ngắn quá."]
    ctx = _ctx(total_min_seconds=300, total_max_seconds=3600)
    violations = check_total_duration(segments, ctx)
    assert any(v.rule_id == "SPEECH_DURATION_TOTAL" for v in violations)
