"""Round 7: shop-availability questions keep the commerce fallback; "ngu nhu bo" is abuse."""

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated
from backend.application.script_authoring.answer_context import question_kind

FACTS = ["Áo khoác denim giá một trăm nghìn", "Có ba màu trắng xanh đen"]
SETS = load_all_curated_patterns()

GOODS = (
    "máy xay|vỏ điện thoại|con chuột|ốp lưng|sạc dự phòng|kẹp tóc|dép đi trong nhà|nồi chiên|"
    "bàn phím|tai nghe|cốc giữ nhiệt|gối ôm|chăn ga|rèm cửa|thảm trải sàn|đèn ngủ|quạt mini|"
    "ấm đun nước|bình giữ nhiệt|khăn tắm|bàn chải|kem chống nắng|sữa rửa mặt|nước giặt|"
    "balo|ví da|thắt lưng|mũ lưỡi trai|kính mát|đồng hồ|vòng tay|dây chuyền|nhẫn|tất cổ cao"
).split("|")


def _fold(text: str) -> str:
    from backend.application.safety_gate.text_matching import fold_text

    return fold_text(text)


@pytest.mark.parametrize("good", GOODS)
@pytest.mark.parametrize("host", ["chị", "shop", "em", "bạn", "admin ơi"])
@pytest.mark.parametrize("ascii_only", [False, True])
def test_shop_has_item_question_is_commerce(good, host, ascii_only):
    text = f"{host} có {good} không?"
    if ascii_only:
        text = _fold(text)
    assert question_kind(text, FACTS) in {"commerce", "current_product", "other_product"}, text


@pytest.mark.parametrize(
    "text",
    [
        "em có sạc dự phòng không ạ",
        "chị có máy xay không?",
        "chị có vỏ điện thoại không?",
        "chị có con chuột không?",
        "chi co may xay khong",
        "shop bán nồi chiên không?",
    ],
)
def test_round7_reviewer_cases(text):
    assert question_kind(text, FACTS) in {"commerce", "current_product", "other_product"}


@pytest.mark.parametrize(
    "text",
    [
        "chị có chồng chưa?",
        "chị có người yêu không?",
        "chị có con không?",
        "chị có thích mua sắm không?",
        "chị quê ở đâu",
        "bạn có khỏe không?",
        "chị có vui không",
        "chị có buồn không",
    ],
)
def test_private_or_greeting_never_gets_commerce_fallback(text):
    assert question_kind(text, FACTS) not in {"commerce", "current_product", "other_product"}


@pytest.mark.parametrize(
    "text",
    [
        "ngu như bò",
        "ngu nhu bo",
        "ngu như chó",
        "ngu như lợn",
        "mày ngu như bò",
        "thật là ngu như bò luôn",
        "ngu quá",
    ],
)
def test_ngu_comparison_is_abuse(text):
    assert match_curated(text, SETS), text


@pytest.mark.parametrize(
    "text",
    [
        "ngủ như heo",
        "đồ ngủ",
        "áo ngủ",
        "chị ngủ chưa",
        "hôm nay em ngu som",
        "em đi ngu đây",
        "do ngu cho be",
        "ao ngu cho be",
        "bo do ngu gia bao nhieu",
        "shop co do ngu cho be khong",
        "nguyên liệu gì vậy",
    ],
)
def test_sleep_and_goods_text_is_not_abuse(text):
    assert not match_curated(text, SETS), text
