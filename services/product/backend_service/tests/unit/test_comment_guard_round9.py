"""Round 9: bare "lao" abuse is blocked again; walnuts and kids goods are not abuse."""

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated

SETS = load_all_curated_patterns()


@pytest.mark.parametrize(
    "text",
    [
        "hon lao",
        "may lao",
        "thang lao",
        "hon lao shop co ship khong?",
        "mày láo vừa thôi",
        "thằng lao",
        "nhắm vào trẻ em",
    ],
)
def test_abuse_is_blocked(text):
    assert match_curated(text, SETS), text


@pytest.mark.parametrize(
    "text",
    [
        "hạt óc chó giá bao nhiêu",
        "hat oc cho con khong shop",
        "nhân óc chó còn hàng không",
        "tất trẻ em size nào vậy",
        "tat tre em con khong",
        "áo trẻ em size 5 giá sao",
        "đồ lao động còn không",
        "do lao dong gia bao nhieu",
        "quần áo lao công có size XL không",
        "đồ cho trẻ em",
    ],
)
def test_goods_are_not_blocked(text):
    assert not match_curated(text, SETS), text
