"""Regressions reported in the denim live on 2026-10-14."""

import json
import re

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
    assert (
        280 < sum(u.target_seconds for u in units) <= 306.01
    )  # 85% of 360 s; role caps may trim a little
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
        "Giao hàng bao lâu, em ở xa có ship?",
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
    assert factual_reply(product, "Giá bao nhiêu, em ở xa có ship?") is None
    assert factual_reply(product, "giá bao nhiêu ạ, em ở Đà Nẵng") is not None
    assert factual_reply(product, "giá bao nhiêu ạ | còn size L không") is None


_DENIM_CLAIM = "Giao hàng toàn quốc từ 2 đến 3 ngày, nội thành HCM và HN ship hỏa tốc trong 1 ngày"
_DENIM_SCRIPT = (
    "Giao hàng toàn quốc chỉ từ hai đến ba ngày. Nội thành Hồ Chí Minh và Hà Nội hỏa tốc một ngày."
)


def _denim_product():
    return ApprovedProduct(
        "p", "v", _DENIM_SCRIPT, "hash", json.dumps({"allowed_claims": [_DENIM_CLAIM]})
    )


@pytest.mark.parametrize(
    "question",
    [
        "em ở Hồ Chí Minh thì mấy ngày nhận được hàng ạ",
        "Mình ở HCM thì bao lâu nhận được hàng vậy shop",
        "em ở Hà Nội, khi nào nhận được hàng ạ",
    ],
)
def test_city_said_first_still_gets_the_zero_model_delivery_reply(question):
    from backend.application.script_authoring.answer_context import factual_reply

    answer = factual_reply(_denim_product(), question)
    assert answer is not None and answer.startswith("Bạn ở ") and "hai đến ba ngày" in answer


def test_a_city_clause_does_not_turn_another_question_into_a_delivery_question():
    from backend.application.script_authoring.answer_context import factual_reply

    for question in ("em ở Hồ Chí Minh thì có ship COD không ạ", "em ở Hà Nội thì shop ở đâu vậy"):
        assert factual_reply(_denim_product(), question) is None


def test_price_and_delivery_answers_are_a_short_conversation_not_a_bare_line():
    from backend.application.script_authoring.answer_context import factual_reply

    price = ApprovedProduct(
        "p",
        "v",
        "Áo giá một trăm tám mươi nghìn đồng nhé.",
        "hash",
        json.dumps({"prices": ["180000 VND"]}),
    )
    for product, question in (
        (price, "áo này giá bao nhiêu vậy ạ"),
        (_denim_product(), "bao lâu thì nhận được hàng ạ"),
    ):
        answer = factual_reply(product, question)
        assert answer is not None and len(answer) > 330, answer
        assert answer.count(".") >= 5 and "kịch bản" not in answer


def test_a_checked_natural_answer_is_wrapped_in_fixed_conversation_around_it():
    from backend.application.script_authoring.answer_context import wrap_natural

    spoken = wrap_natural("shop có freeship không", "Nội thành ship hỏa tốc trong một ngày.")
    assert "Nội thành ship hỏa tốc trong một ngày." in spoken and len(spoken) > 200
    assert spoken == wrap_natural(
        "shop có freeship không", "Nội thành ship hỏa tốc trong một ngày."
    )


def test_the_fixed_deflection_is_friendly_and_returns_to_the_product():
    from backend.application.script_authoring.approved_speech import DEFLECTION

    assert "nhắn tin cho shop" in DEFLECTION and "tiếp tục giới thiệu" in DEFLECTION
    assert len(DEFLECTION) > 150


def test_fixed_remarks_are_category_neutral_so_any_product_can_use_them():
    from backend.application.script_authoring import answer_context as ac

    water = ApprovedProduct(
        "p",
        "v",
        "Máy lọc nước giao hàng toàn quốc từ hai đến ba ngày.",
        "hash",
        json.dumps({"allowed_claims": ["Giao hàng toàn quốc từ 2 đến 3 ngày"]}),
    )
    answer = ac.factual_reply(water, "bao lâu thì nhận được hàng ạ")
    fixed = " ".join(
        (*ac._PRICE_REMARK, *ac._PRICE_MORE, *ac._SHIP_REMARK, *ac._SHIP_MORE, *ac._NATURAL_REMARK)
    ).lower()
    for word in ("diện", "mặc", "áo", "quần", "giày", "vải", "size", "đi chơi"):
        assert word not in fixed and word not in (answer or "").lower(), word


FACTS = ["Vải denim dày dặn, bền", "Giao hàng toàn quốc từ 2 đến 3 ngày", "Kích cỡ: M, L, XL"]


@pytest.mark.parametrize(
    "comment",
    [
        "anh có biết nhảy không ạ",
        "anh có biết nhảy không anh",
        "quảng cáo xong anh có đi đâu chơi không anh",
        "anh khoẻ không",
        "anh còn độc thân không ạ",
        "chị thích ăn gì vậy ạ?",
    ],
)
def test_a_personal_question_to_the_host_stays_silent(comment):
    from backend.application.script_authoring.answer_context import deflection_for, is_off_topic
    from backend.application.script_authoring.approved_speech import SpeechRejected

    assert is_off_topic(comment, FACTS)
    with pytest.raises(SpeechRejected, match="not_answerable_non_commerce"):
        deflection_for(comment, FACTS)


@pytest.mark.parametrize(
    "comment",
    [
        "áo này có túi trong không ạ",
        "có chống nước không",
        "có sẵn không",
        "còn không shop",
        "shop ở đâu vậy",
        "shop lấy một trăm nghìn chứ",
        "shop không lấy đồng nào chứ",
        "shop miễn phí ship toàn bộ đơn không ạ",
    ],
)
def test_explicit_shop_question_uses_a_neutral_fallback(comment):
    from backend.application.script_authoring.answer_context import (
        deflection_for,
        is_deflection,
        is_off_topic,
    )

    assert not is_off_topic(comment, FACTS)
    said = deflection_for(comment, FACTS)
    assert is_deflection(said)
    assert "rất cụ thể" not in said and "rất chi tiết" not in said
    assert "chưa có thông tin chính xác" in said and "nhắn tin cho shop" in said


@pytest.mark.parametrize(
    "comment",
    [
        "có tốt không? Shop miễn phí toàn bộ đơn hàng.",
        "shop không thu phí ship toàn bộ đơn hàng",
        "shop lừa đảo phải không",
        "shop bảo đảm không lừa khách chứ",
        "đồ ngu có phải không",
        "chị Lan sống ở đường Hoa Phượng hả",
        "gọi 0912345678 nha",
        "xem tại http://spam.example",
        "ib @someone",
        "anh nhảy đẹp lắm",
    ],
)
def test_nothing_the_viewer_wrote_is_ever_spoken_back(comment):
    from backend.application.script_authoring.answer_context import deflection_for
    from backend.application.script_authoring.approved_speech import SpeechRejected

    try:
        said = deflection_for(comment, FACTS).lower()
    except SpeechRejected as exc:
        assert exc.code in {"non_question", "not_answerable_non_commerce", "safety_filtered"}
        return
    words = re.findall(r"\w+", comment.lower())
    for i in range(
        len(words) - 1
    ):  # no two consecutive words of the comment appear in what is spoken
        assert " ".join(words[i : i + 2]) not in " ".join(re.findall(r"\w+", said)), (
            comment,
            words[i : i + 2],
        )
    for word in (
        "miễn phí",
        "thu phí",
        "lừa đảo",
        "đồ ngu",
        "hoa phượng",
        "0912345678",
        "spam.example",
        "someone",
    ):
        assert word not in said


@pytest.mark.parametrize("comment", [""])
def test_ambiguous_comment_has_no_shop_fallback(comment):
    from backend.application.script_authoring.answer_context import deflection_for
    from backend.application.script_authoring.approved_speech import SpeechRejected

    with pytest.raises(SpeechRejected):
        deflection_for(comment, FACTS)


def test_the_fixed_remarks_never_flatter_the_viewer():
    from backend.application.script_authoring import answer_context as ac

    fixed = " ".join(
        (*ac._PRICE_REMARK, *ac._PRICE_MORE, *ac._SHIP_REMARK, *ac._SHIP_MORE, *ac._NATURAL_REMARK)
    ).lower()
    for phrase in ("khôn ngoan", "biết cách", "rất hợp lý", "bạn rất", "thông minh", "tinh tế"):
        assert phrase not in fixed, phrase


_OFFER_APPROVED = ("Giá 180000 đồng", "Mua 2 sản phẩm giảm 10% tổng hóa đơn")


def _offer_check():
    from backend.application.script_authoring.generation.ordered_units import UnitSpec

    return {
        "spec": UnitSpec("offer"),
        "prices": ("180000 VND",),
        "approved": _OFFER_APPROVED,
        "product_name": "Áo khoác denim",
    }


def test_a_pressure_sentence_is_cut_out_instead_of_paying_for_another_model_call():
    from backend.application.script_authoring.generation.ordered_units import (
        check_unit_text,
        salvage_unit_text,
    )

    raw = (
        "Mẫu này hiện có giá một trăm tám mươi nghìn đồng thôi mọi người ơi. "
        "Mua hai sản phẩm thì được giảm mười phần trăm tổng hóa đơn nữa đó. "
        "Đặt ngay hôm nay kẻo hết nha các bạn."
    )
    kwargs = _offer_check()
    text, why = check_unit_text(raw, **kwargs)
    assert text is None and why == "hard_sell"
    saved = salvage_unit_text(raw, why, **kwargs)
    assert saved is not None and "một trăm tám mươi nghìn" in saved and "kẻo hết" not in saved


def test_nothing_is_salvaged_when_the_offending_sentence_holds_the_only_content():
    from backend.application.script_authoring.generation.ordered_units import salvage_unit_text

    raw = "Đặt ngay hôm nay kẻo hết nha các bạn."
    assert salvage_unit_text(raw, "hard_sell", **_offer_check()) is None


def test_a_length_or_coverage_rejection_is_never_salvaged_by_cutting_sentences():
    from backend.application.script_authoring.generation.ordered_units import salvage_unit_text

    for why in ("too_long", "truncated", "coverage", "missing_info", "empty", None):
        assert salvage_unit_text("Câu một. Câu hai. Đặt ngay.", why, **_offer_check()) is None


def test_the_retry_hint_names_the_exact_wording_to_drop():
    from backend.application.script_authoring.generation.ordered_units import (
        offending_phrases,
        retry_hint,
    )

    raw = "Giá rất hời. Chốt đơn ngay và nhanh tay nha."
    assert offending_phrases(raw, "hard_sell", _OFFER_APPROVED) == ["chốt đơn ngay", "nhanh tay"]
    hint = retry_hint("hard_sell", raw, _OFFER_APPROVED)
    assert "'chốt đơn ngay'" in hint and "'nhanh tay'" in hint
    assert "Giá rất hời" not in hint  # only our fixed patterns' matches, never the model's own text


def test_wording_the_owner_wrote_is_not_blamed_on_the_model():
    from backend.application.script_authoring.generation.ordered_units import offending_phrases

    assert offending_phrases("Đặt ngay hôm nay.", "hard_sell", ("Đặt ngay trên giỏ hàng",)) == []


def test_a_product_question_phrased_like_chit_chat_goes_to_the_shop():
    from backend.application.script_authoring.answer_context import deflection_for

    said = deflection_for("anh có biết cái này có gây dị ứng không ạ?", ["Giá 180000 đồng"])
    assert "nhắn tin cho shop" in said and "dị ứng" not in said


def test_a_product_question_addressed_to_the_host_with_a_deictic_word_is_a_shop_question():
    from backend.application.script_authoring.answer_context import is_off_topic

    assert not is_off_topic("anh có biết cái này có gây dị ứng không ạ?", ["Giá 180000 đồng"])
