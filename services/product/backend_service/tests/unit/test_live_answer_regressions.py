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
    assert "mới vào" in after and "mới vào" not in before
    assert "kịch bản" not in before + after
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


def test_delivery_question_with_city_and_filler_words_is_answered_without_the_model():
    from backend.application.script_authoring.answer_context import factual_reply

    question = "bao lâu thì nhận được hàng ạ, em ở Hồ Chí Minh"
    assert route_hints(question, []).intent_candidates == ["shipping"]
    claim = "Giao hàng toàn quốc từ 2 đến 3 ngày, nội thành HCM và HN ship hỏa tốc trong 1 ngày"
    script = "Giao hàng toàn quốc chỉ từ hai đến ba ngày. Nội thành Hồ Chí Minh và Hà Nội hỏa tốc một ngày."
    product = ApprovedProduct("p", "v", script, "hash", json.dumps({"allowed_claims": [claim]}))
    answer = factual_reply(product, question)
    assert answer.startswith("Bạn ở Hồ Chí Minh") and "hai đến ba ngày" in answer
    assert len(answer) > 200 and "kịch bản" not in answer


def test_cash_on_delivery_question_is_not_a_delivery_time_question():
    assert route_hints("thanh toán khi nhận hàng được không", []).intent_candidates != ["shipping"]


def test_natural_answer_missing_only_its_full_stop_is_closed_but_a_cut_one_is_not():
    from backend.application.script_authoring.approved_speech import _close_sentence

    assert _close_sentence("Mình giao hàng toàn quốc nhé") == "Mình giao hàng toàn quốc nhé."
    assert (
        _close_sentence("Mình giao hàng toàn quốc từ hai đến")
        == "Mình giao hàng toàn quốc từ hai đến"
    )


def test_unit_length_follows_the_measured_voice_pace_and_the_share_of_the_live():
    from backend.application.script_authoring.generation.ordered_units import (
        ProductBrief,
        SessionBrief,
        build_unit_prompt,
        plan_units,
    )

    claims = tuple(f"Thông tin số {n}." for n in range(1, 8))
    product = ProductBrief("p", "Áo denim", claims=claims)
    units = plan_units(product, first=True, last=True, target_duration_s=360)
    assert 250 < sum(u.target_seconds for u in units) <= 270.01  # role caps may trim a little
    middle = next(i for i, u in enumerate(units) if u.role == "highlight")
    prompt = build_unit_prompt(SessionBrief(), product, units, middle)
    low = int(units[middle].target_seconds * 16 * 0.85)
    assert f"khoảng {low} đến" in prompt


@pytest.mark.parametrize(
    "question",
    [
        "Nhận được hàng rồi mới trả tiền được không?",
        "Bao lâu có hàng lại vậy shop?",
        "Bao lâu nhận được tiền hoàn?",
        "Giao hàng rồi mới đưa tiền được không?",
        "Bao lâu nhận được khoản bồi thường?",
        "Shop bao giờ nhận đơn trở lại?",
        "Bao lâu nhận được xác nhận đơn hàng?",
        "Shop bao giờ nhận đơn hàng sau Tết?",
        "Shop bao giờ nhận hàng mới về?",
        "Từ ngày nhận hàng thì bao lâu hết bảo hành?",
        "Giao hàng rồi bao lâu mới được giặt áo?",
        "Giao hàng bao lâu, shop ở đâu?",
        "Giao hàng bao lâu và size L còn không?",
    ],
)
def test_payment_and_restock_questions_never_get_the_delivery_time_reply(question):
    from backend.application.script_authoring.answer_context import factual_reply

    claim = "Giao hàng toàn quốc từ 2 đến 3 ngày"
    product = ApprovedProduct(
        "p", "v", "Giao hàng hai đến ba ngày.", "hash", json.dumps({"allowed_claims": [claim]})
    )
    assert factual_reply(product, question) is None


def test_only_the_city_the_viewer_lives_in_is_echoed():
    from backend.application.script_authoring.answer_context import _city

    assert _city("bao lâu thì nhận được hàng, em ở Hồ Chí Minh") == "Hồ Chí Minh"
    assert _city("Bao lâu giao tới Hà Nội? Em ở Đà Nẵng.") is None
    assert _city("em ở Hà Nội hay Hồ Chí Minh cũng được") is None


def test_unfinished_replies_are_not_closed_by_a_stray_particle():
    from backend.application.script_authoring.approved_speech import _close_sentence

    assert (
        _close_sentence("Mình giao hàng toàn quốc rồi nên mình luôn")
        == "Mình giao hàng toàn quốc rồi nên mình luôn"
    )
    assert _close_sentence("Mình giao hàng toàn quốc rồi,") == "Mình giao hàng toàn quốc rồi,"


def test_a_plain_delivery_time_question_still_gets_the_reply():
    from backend.application.script_authoring.answer_context import factual_reply

    claim = "Giao hàng toàn quốc từ 2 đến 3 ngày"
    product = ApprovedProduct(
        "p",
        "v",
        "Giao hàng toàn quốc chỉ từ hai đến ba ngày.",
        "hash",
        json.dumps({"allowed_claims": [claim]}),
    )
    for question in (
        "Thời gian giao hàng bao lâu?",
        "Đặt hàng thì bao lâu nhận được?",
        "bao lâu thì nhận được hàng ạ",
        "Giao hàng mất bao nhiêu ngày?",
    ):
        assert factual_reply(product, question) is not None, question


def test_price_shortcut_needs_a_price_question_and_every_clustered_comment_to_agree():
    from backend.application.script_authoring.answer_context import factual_reply

    product = ApprovedProduct(
        "p",
        "v",
        "Áo giá một trăm tám mươi nghìn đồng nhé.",
        "hash",
        json.dumps({"prices": ["180000 VND"]}),
    )
    assert factual_reply(product, "áo này giá bao nhiêu vậy ạ") is not None
    assert factual_reply(product, "giá bao nhiêu ạ | bao nhiêu tiền vậy shop") is not None
    assert factual_reply(product, "Áo này có bao nhiêu túi?") is None
    assert factual_reply(product, "Giá bao nhiêu, shop ở đâu?") is None
    assert factual_reply(product, "giá bao nhiêu ạ | còn size L không") is None
