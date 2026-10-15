"""Regressions reported in the denim live on 2026-10-14."""

import json

import pytest

from backend.application.director.routing import route_hints
from backend.application.script_authoring.approved_speech import ApprovedProduct


@pytest.mark.parametrize(
    "question",
    [
        "Đặt hàng thì bao lâu nhận được?",
        "Mua hôm nay mấy ngày nhận hàng?",
        "Thời gian giao hàng bao lâu?",
        "Khi nào nhận được áo?",
    ],
)
def test_delivery_question_is_not_purchase_intent(question):
    assert route_hints(question, []).intent_candidates == ["shipping"]


def test_other_policy_duration_does_not_hide_shipping_fact():
    shipping = "Giao hàng toàn quốc từ 2 đến 3 ngày"
    product = ApprovedProduct(
        "p",
        "v",
        (
            "Bên mình giao hàng toàn quốc rất nhanh chỉ từ hai đến ba ngày là tới tận tay.\n\n"
            "Miễn phí đổi trả trong mười lăm ngày đầu kể từ khi nhận hàng."
        ),
        "hash",
        json.dumps(
            {"allowed_claims": [shipping, "Miễn phí đổi trả trong 15 ngày đầu kể từ khi nhận hàng"]}
        ),
    )
    assert "Giao hàng toàn quốc từ hai đến ba ngày" in product.answers()


def test_contextual_price_answer_uses_completed_speech_only():
    from backend.application.script_authoring.answer_context import factual_reply

    spoken = "Mẫu này giá một trăm tám mươi nghìn đồng nhé."
    product = ApprovedProduct("p", "v", spoken, "hash", json.dumps({"prices": ["180000 VND"]}))
    before = factual_reply(product, "Giá bao nhiêu?")
    after = factual_reply(product, "Giá bao nhiêu?", spoken)
    assert "trả lời câu hỏi" in before
    assert "nhắc lại" in after
    assert "một trăm tám mươi nghìn đồng" in before and "một trăm tám mươi nghìn đồng" in after
    assert factual_reply(product, "Giá gốc bao nhiêu?") is None
    assert factual_reply(product, "Mua hai áo giá bao nhiêu?") is None


def test_six_minute_plan_adds_spaced_fact_recaps_and_duration_instructions():
    from backend.application.script_authoring.generation.ordered_units import (
        ProductBrief,
        SessionBrief,
        plan_units,
        build_unit_prompt,
    )

    claims = (
        "Denim dày dặn.",
        "Có size M, L, XL.",
        "Giao hàng từ 2 đến 3 ngày.",
        "Đổi trả trong 15 ngày.",
        "Không giặt nước nóng.",
    )
    product = ProductBrief("p", "Áo denim", claims=claims)
    short = plan_units(product, first=True, last=True)
    long = plan_units(product, first=True, last=True, target_duration_s=360)
    assert len(long) > len(short)
    assert all(u.role not in ("opening", "closing", "intro") for u in long if u.revisit)
    assert sum(u.with_cta for u in long) == 1
    assert all(sum(c in u.claims for u in long) <= 2 for c in claims)
    index = next(i for i, u in enumerate(long) if u.revisit)
    prompt = build_unit_prompt(SessionBrief(), product, long, index)
    assert "Độ dài mục tiêu" in prompt and "diễn đạt KHÁC" in prompt


def test_denim_delivery_reply_keeps_both_general_and_city_policies():
    from backend.application.script_authoring.answer_context import factual_reply

    claim = "Giao hàng toàn quốc từ 2 đến 3 ngày, nội thành HCM và HN ship hỏa tốc trong 1 ngày"
    script = (
        "Bên mình giao hàng toàn quốc rất nhanh chỉ từ hai đến ba ngày là tới tận tay rồi. "
        "Riêng các bạn ở khu vực nội thành Hà Nội và Thành phố Hồ Chí Minh thì bên mình "
        "hỗ trợ ship hỏa tốc trong vòng một ngày."
    )
    product = ApprovedProduct("p", "v", script, "hash", json.dumps({"allowed_claims": [claim]}))
    answer = factual_reply(product, "Đặt hàng thì bao lâu nhận được?", script)
    assert answer is not None and "hai đến ba ngày" in answer and "một ngày" in answer
