"""Opt-in claim relaxation and advisory owner-facing full-script checks."""

import pytest

from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.context import relaxed_gate_enabled
from backend.application.script_authoring.gate.engine import (
    ScriptGate,
    default_full_script_rules,
    default_segment_rules,
)
from backend.application.script_authoring.gate.registry import ScriptRuleRegistry
from backend.application.script_authoring.gate.results import Severity
from backend.application.script_authoring.gate.rules.commerce_claims import (
    check_factual_claims,
    check_price_claims,
)
from backend.application.script_authoring.gate.rules.full_script import check_contradictory_claims
from backend.application.script_authoring.generation.reason_text import reason_text
from backend.application.script_authoring.generation.unit_brief import fallback_reason_code


def _ctx(**facts) -> ScriptGateContext:
    return ScriptGateContext(facts=ProductFacts(**facts))


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "YeS", "on", "ON"])
def test_flag_is_read_at_call_time(monkeypatch, value):
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    assert not relaxed_gate_enabled()
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", value)
    assert relaxed_gate_enabled()
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "false")
    assert not relaxed_gate_enabled()


def test_descriptive_claim_defaults_to_error(monkeypatch):
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    violations = check_factual_claims("Máy dùng rất dễ dàng.", _ctx())
    assert len(violations) == 1
    assert violations[0].severity == Severity.ERROR


@pytest.mark.parametrize(
    ("text", "severity"),
    [
        ("Máy dùng rất dễ dàng.", Severity.WARNING),
        ("Thiết kế đơn giản nhưng rất thanh lịch", Severity.WARNING),
        ("cũng không cam kết độ bền trong tình huống đó", Severity.ERROR),
        ("Sản phẩm giúp trị mụn hiệu quả", Severity.ERROR),
        ("cũng không cam kết độ bền", Severity.ERROR),
        ("Bảo hành trọn đời", Severity.ERROR),
        ("Sản phẩm chữa bệnh", Severity.ERROR),
        ("Sản phẩm đạt chuẩn FDA", Severity.ERROR),
        ("không cam kết nhưng đảm bảo hiệu quả", Severity.ERROR),
        ("Ổn định đường huyết", Severity.ERROR),
        ("Thiết kế giúp hết đau lưng", Severity.ERROR),
        ("Đơn giản mà hết nhức mỏi", Severity.ERROR),
        ("Chất lượng tốt cho sức khỏe", Severity.ERROR),
        ("Chất lượng chuẩn y khoa", Severity.ERROR),
        ("Chất lượng được kiểm nghiệm", Severity.ERROR),
        ("Thành phần đã qua kiểm nghiệm lâm sàng", Severity.ERROR),
        ("Giúp giảm nếp nhăn", Severity.ERROR),
        ("Hỗ trợ xương khớp", Severity.ERROR),
        ("Dùng được cho bà bầu", Severity.ERROR),
        ("Được bác sĩ khuyên dùng", Severity.ERROR),
        ("Đạt tiêu chuẩn châu Âu", Severity.ERROR),
        ("Không gây kích ứng", Severity.ERROR),
        ("Thành phần không chứa paraben", Severity.ERROR),
    ],
)
def test_relaxed_word_claim_severity(monkeypatch, text, severity):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(text, _ctx(product_name="Máy"))
    assert len(violations) == 1
    assert violations[0].rule_id == "CLAIM_FACTUAL"
    assert violations[0].severity == severity


@pytest.mark.parametrize(
    "text",
    [
        "Thành phần từ nghệ giúp phòng ngừa ung thư",
        "Thành phần giúp giải độc gan",
        "Thành phần bổ thận",
        "Thành phần hạ mỡ máu",
        "Thành phần giảm cholesterol",
        "Thành phần trị táo bón",
        "Thành phần phòng covid",
        "Thành phần hỗ trợ cai thuốc lá",
        "Thành phần tăng sinh lý",
        "Thành phần lợi sữa",
        "Thành phần chắc khỏe xương cho bé",
        "Thành phần không tác dụng phụ",
        "Thành phần không gây dị ứng",
        "Thành phần không gây nghiện",
        "Thành phần không đau",
        "Thành phần thiên nhiên dùng được cho bé",
        "Nguyên liệu giúp bé thông minh",
        "tiết kiệm hai trăm nghìn đồng so với giá gốc",
        "Thiết kế đẹp mà tiết kiệm được năm trăm nghìn",
        "Làm sạch chín mươi chín phần trăm vết bẩn",
        "Tiết kiệm điện gấp ba lần",
        "Tiết kiệm một nửa tiền điện",
        "Thiết kế độc quyền",
        "Chất lượng Nhật Bản chuẩn xuất khẩu",
        "Nguyên liệu nhập khẩu Hàn Quốc",
        "Tiện lợi hơn hẳn các hãng khác",
        "Thiết kế được cấp bằng sáng chế",
        "Chất lượng hàng chính hãng",
        "Công suất mạnh nhất thị trường",
        "Thành phần không chứa chất bảo quản",
    ],
)
def test_structural_claims_are_never_demoted(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(text, _ctx())
    assert [(v.rule_id, v.severity) for v in violations] == [("CLAIM_FACTUAL", Severity.ERROR)]


@pytest.mark.parametrize(
    "text",
    [
        "Máy dùng rất dễ dàng.",
        "Thiết kế đơn giản nhưng rất thanh lịch",
        "Mọi người thấy thế nào về sự tiện lợi này",
        "Các bạn có thể dễ dàng mang theo bên mình mà không thấy cồng kềnh "
        "hay vướng víu gì đúng không",
        "Cách chốt đơn rất đơn giản: bình luận chữ Chốt",
        "Đôi khi đơn giản là một lợi thế về thao tác",
        "Từ chiếc balo UrbanFold tiện lợi",
        "bạn có thể cân nhắc thiết kế này",
        "Thiết kế gọn, để trong phòng khách rất hợp",
        "Thiết kế tiện lợi cho người dùng",
        "Ngăn chứa đồ rộng rãi, thiết kế tiện lợi",
    ],
)
def test_descriptive_production_phrases_remain_warnings(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(text, _ctx(product_name="Máy Balo UrbanFold"))
    assert violations
    assert all(v.severity == Severity.WARNING for v in violations)


@pytest.mark.parametrize(
    "phrase",
    [
        "phòng khách",
        "phòng ngủ",
        "phòng tắm",
        "văn phòng",
        "giải thích",
        "giải đáp",
        "giải trí",
        "chống trượt",
        "chống sốc",
    ],
)
def test_harmless_effect_compounds_remain_descriptive(monkeypatch, phrase):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thiết kế {phrase.upper()}"
    severity = Severity.WARNING if phrase == "phòng khách" else Severity.ERROR
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [severity]
    assert [v.severity for v in check_factual_claims(f"{text} giúp bé", _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize(
    "phrase",
    [
        "bổ sung vitamin",
        "chống nước",
        "tăng giảm",
        "giảm giá",
        "chứng thực",
        "tin dùng",
        "xuất xứ",
        "vượt trội",
        "số một",
        "số 1",
    ],
)
def test_effects_and_assertions_block_demotion(monkeypatch, phrase):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thiết kế {phrase.upper()}"
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize(
    "quantity",
    [
        "trăm",
        "nghìn",
        "ngàn",
        "triệu",
        "tỷ",
        "tỉ",
        "mươi",
        "phần trăm",
        "gấp đôi",
        "gấp ba",
        "nửa",
        "một lần",
        "hai tháng",
        "ba năm",
        "bốn ngày",
        "năm tuần",
        "sáu giờ",
        "bảy phút",
        "tám giây",
        "chín kg",
        "mười ký",
        "hai cân",
        "ba lít",
        "chục",
        "lăm",
        "mốt",
        "thập kỷ",
        "thập niên",
        "kilowatt",
        "oát",
        "watt",
        "phần hai",
        "phần ba",
        "phần tư",
        "phần năm",
        "phần mười",
    ],
)
def test_written_quantities_block_demotion(monkeypatch, quantity):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thiết kế {quantity.upper()}"
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize("phrase", ["gấpđôi", "giúpđỡ", "nhấtquán"])
def test_unknown_joined_words_fail_closed(monkeypatch, phrase):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert [v.severity for v in check_factual_claims(f"Thiết kế {phrase}", _ctx())] == [
        Severity.ERROR
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Dễ dàng thay thế thuốc.",
        "Thiết kế đơn giản thay thế thuốc ngủ.",
        "Dễ dàng hết say xe.",
        "Đơn giản mà khỏe mạnh.",
        "Thiết kế tiện lợi khỏe re.",
        "Thiết kế đơn giản dùng cho thai phụ.",
        "Thiết kế đơn giản phù hợp người ăn kiêng.",
        "Thiết kế tiện lợi không lo cháy nổ.",
        "Thiết kế tiện lợi không lo điện giật.",
        "Thiết kế vô trùng tuyệt đối.",
        "Thiết kế tiện lợi kháng nước.",
        "Thiết kế được cấp phép lưu hành.",
        "Thiết kế được chuyên gia khuyên dùng.",
        "Thiết kế đạt giải thưởng quốc tế.",
        "Thiết kế tiện lợi bán chạy hàng đầu.",
        "Thiết kế tiện lợi top một thị trường.",
        "Thiết kế tiện lợi hàng Nhật nội địa.",
        "Thiết kế tiện lợi miễn phí vận chuyển.",
        "Thiết kế tiện lợi tặng kèm quà.",
        "Thiết kế bền bỉ trọn đời.",
        "Thiết kế tiện lợi, chống ung thư.",
        "Tiện lợi, uống vào là khỏe.",
        "Thiết kế thông minh, gấp gọn dễ dàng",
        "Một giải pháp tiện lợi cho dân văn phòng",
        "Thiết kế nhỏ bé xinh xắn",
        "Thiết kế bổ sung thêm ngăn phụ tiện lợi",
        "Thiết kế chống trơn trượt tiện lợi",
        "Đặt ở phòng bếp rất tiện lợi",
        "Thiết kế bổ sung",
    ],
)
def test_unapproved_vocabulary_prevents_demotion(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(text, _ctx())
    assert any(v.severity == Severity.ERROR for v in violations)


@pytest.mark.parametrize(
    "facts",
    [{"product_name": "Balo UrbanFold"}, {"allowed_claims": ("Balo", "UrbanFold")}],
)
def test_owner_vocabulary_can_be_used_in_descriptive_claims(monkeypatch, facts):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = "Từ chiếc balo UrbanFold tiện lợi"
    assert [v.severity for v in check_factual_claims(text, _ctx(**facts))] == [Severity.WARNING]
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize(
    "text",
    [
        "không tác dụng phụ",
        "không có tác dụng phụ",
        "không gây khó chịu",
        "không chứa đường",
        "không đau",
    ],
)
def test_safety_phrases_are_non_negatable_and_can_be_approved(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    negated = f"Sản phẩm chưa {text}"
    text = f"Sản phẩm {text}"
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]
    assert [v.severity for v in check_factual_claims(negated, _ctx())] == [Severity.ERROR]
    assert check_factual_claims(text, _ctx(allowed_claims=(text,))) == []
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "0")
    assert check_factual_claims(text, _ctx()) == []


@pytest.mark.parametrize(
    "text",
    ["Thành phần bổ thận", "Tiết kiệm hai tháng", "Thiết kế độc quyền"],
)
def test_structural_guards_preserve_approval_and_flag_off(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_factual_claims(text, _ctx(allowed_claims=(text,))) == []
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "0")
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize("prefix", ["không", "không hề", "chưa", "chẳng", "không phải"])
def test_strict_claim_negation(monkeypatch, prefix):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(f"cũng {prefix} cam kết độ bền", _ctx())
    assert [v.severity for v in violations] == [Severity.ERROR]


def test_allowed_claim_overlap_still_exempt(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert (
        check_factual_claims("Bảo hành trọn đời", _ctx(allowed_claims=("Bảo hành trọn đời",))) == []
    )


def test_owner_number_is_not_a_price_only_when_relaxed(monkeypatch):
    context = _ctx(allowed_claims=("Pin 5.000 mAh dùng 2 ngày",), prices=("299.000",))
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    original = check_price_claims("Pin 5.000 mAh nhé", context)
    assert len(original) == 1
    assert original[0].severity == Severity.ERROR
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_price_claims("Pin 5.000 mAh nhé", context) == []
    wrong_price = check_price_claims("Giá chỉ 199.000đ", context)
    assert len(wrong_price) == 1
    assert wrong_price[0].severity == Severity.ERROR


@pytest.mark.parametrize(
    ("facts", "text", "flagged"),
    [
        ({"product_name": "Máy 1.200W"}, "Máy 1.200W", False),
        ({"allowed_claims": ("Công suất 1200W",)}, "Máy 1.200W", True),
        ({"allowed_claims": ("Pin 15.000 mAh",)}, "Pin 5.000 mAh", True),
        ({"allowed_claims": ("Pin 5.000 mAh",)}, "Giá 5.000đ", True),
        ({"allowed_claims": ("Giảm 50.000đ khi mua 2",)}, "Giá chỉ 50.000đ", True),
        (
            {"allowed_claims": ("Giá gốc 599.000đ, nay còn 299.000đ",), "prices": ("299.000đ",)},
            "Giá hôm nay 599.000đ",
            True,
        ),
        ({"allowed_claims": ("Phí ship 30k",)}, "Giá 30k", True),
        ({"allowed_claims": ("Pin 5.000 mAh",)}, "Giá chỉ 5.000", True),
        ({"allowed_claims": ("Pin 5.000 mAh",)}, "Pin 5.000 mAh dùng cả ngày", False),
        ({"allowed_claims": ("Công suất 1.200W",)}, "công suất 1.200 W", False),
        ({"allowed_claims": ("Giá 1.200.000 W",)}, "Máy 1.200 W", True),
        ({"allowed_claims": ("Pin 5.000 mAh",)}, "Pin 5.000 W", True),
        ({"allowed_claims": ("Pin 5.000 mAh",)}, "Pin 5.000  mAh", True),
        ({"product_name": "Pin 5.000 MAH"}, "Pin 5.000mah", False),
    ],
)
def test_price_exemption_requires_exact_surface(monkeypatch, facts, text, flagged):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert bool(check_price_claims(text, _ctx(**facts))) == flagged


@pytest.mark.parametrize(
    "unit", ["đ", "₫", "đồng", "vnd", "vnđ", "k", "K", "nghìn", "ngàn", "tr", "triệu"]
)
def test_money_in_approved_claim_never_exempts_price(monkeypatch, unit):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Giá 50.000 {unit}"
    violations = check_price_claims(text, _ctx(allowed_claims=(text,)))
    assert [v.severity for v in violations] == [Severity.ERROR]


@pytest.mark.parametrize(
    ("claim", "text"),
    [
        ("Giảm 500.000 cho đơn đầu tiên", "Giá chỉ 500.000 cho bạn hôm nay"),
        ("Giá gốc 990.000 nay giảm còn 299.000", "Giá 990.000 nay chốt luôn"),
        ("Phí ship 30.000 toàn quốc", "Giá 30.000 toàn quốc"),
        ("Combo 2 hộp 550.000 tiết kiệm", "Mua liền 550.000 tiết kiệm"),
        ("Tặng voucher 100.000 khi mua", "Chỉ 100.000 khi mua hôm nay"),
    ],
)
def test_bare_money_in_approved_claim_never_exempts_price(monkeypatch, claim, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_price_claims(text, _ctx(allowed_claims=(claim,), prices=("299.000",)))
    assert [v.severity for v in violations] == [Severity.ERROR]


@pytest.mark.parametrize(
    "cue", "giá chỉ còn chốt ship giảm tặng voucher combo mua cũ gốc tiền phí đồng bán trả".split()
)
@pytest.mark.parametrize("between", ["", "hôm "])
def test_measurement_with_recent_price_cue_is_not_exempt(monkeypatch, cue, between):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_price_claims(
        f"{cue.upper()} {between}5.000 mAh", _ctx(allowed_claims=("Pin 5.000 mAh",))
    )
    assert [v.severity for v in violations] == [Severity.ERROR]


@pytest.mark.parametrize(
    "unit",
    (
        "mah w kw wh v ml l lít g gr kg mg cm mm m inch gb tb mb hz mhz ghz mp dpi rpm "
        "viên gói miếng cái chiếc lượt vòng độ °c lumen lux ppi fps"
    ).split(),
)
def test_explicit_measurement_units_are_exempt(monkeypatch, unit):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    context = _ctx(allowed_claims=(f"Thông số 5.000{unit}",), prices=("299.000",))
    assert check_price_claims(f"Thông số 5.000 {unit.upper()}", context) == []


@pytest.mark.parametrize("unit", ["watts", "mahx", "mAh2", "gói_hàng", "cho", "tiết"])
def test_unlisted_or_partial_measurement_unit_is_not_exempt(monkeypatch, unit):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thông số 5.000 {unit}"
    assert [v.severity for v in check_price_claims(text, _ctx(allowed_claims=(text,)))] == [
        Severity.ERROR
    ]


@pytest.mark.parametrize("prefix", ["Giá rất tốt. Pin", "Giá rất tốt và pin", "Giá rất tốt pin"])
def test_price_cue_scope_is_two_words_in_script_clause(monkeypatch, prefix):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_price_claims(f"{prefix} 5.000 mAh", _ctx(allowed_claims=("Pin 5.000 mAh",))) == []


@pytest.mark.parametrize(
    "text",
    [
        "Độ bền 10 năm",
        "Tuổi thọ 20 năm",
        "Công suất 2000W",
        "Tiết kiệm 50% điện năng",
        "Nguyên liệu 100% tự nhiên",
        "Làm sạch 99,9% vi khuẩn",
        "Dễ dàng giảm 5kg trong 1 tháng",
        "Thiết kế chống nước IP68",
    ],
)
def test_numeric_word_claims_are_never_demoted(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(text, _ctx())
    assert any(v.rule_id == "CLAIM_FACTUAL" and v.severity == Severity.ERROR for v in violations)


@pytest.mark.parametrize(
    "text",
    [
        "Tăng cường sức đề kháng",
        "An toàn cho bà bầu",
        "Hiệu quả rõ rệt sau 7 ngày",
        "Bảo vệ da khỏi tia UV",
        "Chống lão hóa hiệu quả",
        "Giúp giảm mụn hiệu quả rõ rệt",
    ],
)
@pytest.mark.parametrize("flag", ["0", "1"])
def test_efficacy_and_safety_claims_remain_errors(monkeypatch, text, flag):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", flag)
    assert any(v.severity == Severity.ERROR for v in check_factual_claims(text, _ctx()))


@pytest.mark.parametrize(
    "text",
    [
        "Shop nhận sửa chữa tận nơi",
        "Bình chữa cháy mini",
        "Đảm bảo đúng như hình",
        "Nam giới chọn mẫu này",
        "Logo ISOTYPE",
    ],
)
@pytest.mark.parametrize("flag", ["0", "1"])
def test_everyday_phrases_are_not_health_claims(monkeypatch, text, flag):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", flag)
    assert check_factual_claims(text, _ctx()) == []


@pytest.mark.parametrize(
    "text",
    [
        "Kháng viêm cực tốt",
        "Lành tính không tác dụng phụ",
        "Kháng khuẩn mềm mại",
        "Chữa bệnh nhanh chóng",
        "Điều trị ho",
        "Trị gàu",
        "Chứng nhận ISO",
        "Kiểm định GMP",
        "Hữu cơ organic",
        "Dễ dàng cải thiện làn da",
    ],
)
def test_health_and_non_descriptive_claims_fail_closed(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert any(v.severity == Severity.ERROR for v in check_factual_claims(text, _ctx()))


@pytest.mark.parametrize("prefix", ["không", "không hề", "chưa", "chẳng", "không phải"])
def test_direct_health_negation_is_exempt(monkeypatch, prefix):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_factual_claims(f"Sản phẩm {prefix} chữa ho", _ctx()) == []


@pytest.mark.parametrize("claim", ["kích ứng", "paraben", "độc hại"])
@pytest.mark.parametrize("prefix", ["không", "không hề", "chưa", "chẳng", "không phải"])
def test_safety_claims_cannot_be_excused_by_negation(monkeypatch, claim, prefix):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert [v.severity for v in check_factual_claims(f"Sản phẩm {prefix} {claim}", _ctx())] == [
        Severity.ERROR
    ]


@pytest.mark.parametrize("text", ["Logo IP680", "Logo IP68X", "Logo parabenfree", "Logo virusx"])
def test_health_terms_require_word_boundaries(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_factual_claims(text, _ctx()) == []


@pytest.mark.parametrize(
    "text",
    [
        "Không chữa khỏi thì hoàn tiền",
        "Không chữa khỏi hoàn tiền",
        "Nếu không chữa khỏi sẽ đổi trả",
        "Không chữa khỏi bằng không sẽ đổi trả",
    ],
)
def test_conditional_health_negation_remains_error(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert any(v.severity == Severity.ERROR for v in check_factual_claims(text, _ctx()))


def test_owner_approved_health_claim_is_supported(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(
        "Vải kháng khuẩn mềm mại", _ctx(allowed_claims=("Vải kháng khuẩn",))
    )
    assert not any(v.severity == Severity.ERROR for v in violations)


@pytest.mark.parametrize("prefix", ["không", "không hề", "không phải", "chẳng", "không có"])
@pytest.mark.parametrize("copula", ["", "là "])
@pytest.mark.parametrize(
    ("claim", "negative"),
    [("an toàn", "độc hại"), ("chính hãng", "hàng giả"), ("mới 100%", "đã qua sử dụng")],
)
def test_negated_negative_is_consistent(monkeypatch, prefix, copula, claim, negative):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_contradictory_claims([claim, f"{prefix} {copula}{negative} cho bé."], _ctx()) == []


def test_contradiction_flag_off_and_on(monkeypatch):
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    segments = ["Chất liệu an toàn.", "Không độc hại cho bé."]
    assert [v.severity for v in check_contradictory_claims(segments, _ctx())] == [Severity.ERROR]
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_contradictory_claims(segments, _ctx()) == []
    assert [v.severity for v in check_contradictory_claims(["an toàn", "độc hại"], _ctx())] == [
        Severity.ERROR
    ]


def test_warranty_denial_and_exception_remain_distinct(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert [
        v.severity
        for v in check_contradictory_claims(["Bảo hành chính hãng", "Không bảo hành"], _ctx())
    ] == [Severity.ERROR]
    assert (
        check_contradictory_claims(["Bảo hành chính hãng", "Không bảo hành khi rơi vỡ"], _ctx())
        == []
    )


@pytest.mark.parametrize(
    ("claim", "negative", "severity"),
    [
        ("an toàn", "độc hại", Severity.ERROR),
        ("an toàn", "không phải là độc hại thì là gì", Severity.ERROR),
        ("Chính hãng.", "hàng giả", Severity.ERROR),
        ("mới 100%", "đã qua sử dụng", Severity.ERROR),
        ("bảo hành", "không bảo hành", Severity.ERROR),
    ],
)
@pytest.mark.parametrize("flag", ["0", "1"])
def test_contradiction_severity_by_pair(monkeypatch, claim, negative, severity, flag):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", flag)
    violations = check_contradictory_claims([claim, negative], _ctx())
    assert [v.severity for v in violations] == [severity]


def test_contradiction_condition_is_scoped_to_occurrence_clause(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert (
        check_contradictory_claims(
            ["Chất liệu an toàn.", "Không độc hại cho bé. Thích thì mua."], _ctx()
        )
        == []
    )


@pytest.mark.parametrize(
    ("approved", "spoken"), [("12 tháng", "1 năm"), ("1 năm", "12 tháng"), ("18 tháng", "1,5 năm")]
)
def test_years_and_months_are_equivalent_only_when_relaxed(monkeypatch, approved, spoken):
    context = _ctx(allowed_claims=(f"Bảo hành {approved}",))
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    assert any(
        v.severity == Severity.ERROR for v in check_factual_claims(f"Bảo hành {spoken}", context)
    )
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_factual_claims(f"Bảo hành {spoken}", context) == []
    assert any(
        v.severity == Severity.ERROR for v in check_factual_claims("Bảo hành 6 tháng", context)
    )


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        (["CLAIM_PRICE", "semantic_budget_exhausted"], "facts_mismatch"),
        (["REPETITION_LOCAL", "semantic_budget_exhausted"], "wording_unsuitable"),
        (["guard:coverage", "semantic_budget_exhausted"], "coverage_missing"),
        (["CLAIM_PRICE", "guard:truncated", "semantic_budget_exhausted"], "unfinished_text"),
        (["semantic_budget_exhausted"], "time_budget_exhausted"),
        (["???", "semantic_budget_exhausted"], "time_budget_exhausted"),
        (["CLAIM_PRICE", "llm_failed:TimeoutError"], "time_budget_exhausted"),
        (["CLAIM_PRICE", "llm_failed:LLMDeadlineError"], "time_budget_exhausted"),
    ],
)
def test_budget_cap_preserves_specific_reason(reasons, expected):
    assert fallback_reason_code(reasons) == expected


def test_budget_cap_preserves_specific_display_text():
    assert reason_text(["CLAIM_PRICE", "semantic_budget_exhausted"]) == reason_text(["CLAIM_PRICE"])
    assert reason_text(["REPETITION_LOCAL", "semantic_budget_exhausted"]) == reason_text(
        ["REPETITION_LOCAL"]
    )
    assert reason_text(["CLAIM_PRICE", "llm_failed:TimeoutError"]) == reason_text(
        ["llm_failed:TimeoutError"]
    )


def test_relaxed_promise_of_relief_from_worry_is_not_a_health_claim(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_factual_claims("Đảm bảo khỏi phải lo", _ctx()) == []


@pytest.mark.parametrize(
    "text",
    [
        "Độ bền mười lăm năm",
        "Tuổi thọ cả chục năm",
        "Độ bền cả thập kỷ",
        "Tiết kiệm đến hai phần ba lượng điện",
        "Công suất hai kilowatt",
        "Thành phần không corticoid",
        "Thành phần không cồn",
        "Nguyên liệu không độc tố",
        "Thiết kế không chì",
        "Thành phần collagen trẻ hóa làn da",
        "Làm sạch sâu se khít lỗ chân lông",
        "Làm mềm da mịn màng",
        "Thiết kế ôm lưng không còn cong vẹo cột sống",
        "Làm sạch da không mụn",
        "Chất lượng ngang hàng hiệu",
        "Tiết kiệm điện đáng kể",
        "Thành phần thảo dược quý",
        "Kết quả phụ thuộc vào loại nguyên liệu",
    ],
)
def test_unsupported_performance_and_body_claims_remain_errors(monkeypatch, text):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(text, _ctx())
    assert any(v.severity == Severity.ERROR for v in violations)


@pytest.mark.parametrize(
    "signal",
    [
        "chất lượng",
        "ổn định",
        "độ bền",
        "tuổi thọ",
        "đáng tin cậy",
        "tiết kiệm",
        "tối ưu",
        "nguyên liệu",
        "thành phần",
        "công suất",
        "làm sạch",
        "làm mềm",
    ],
)
@pytest.mark.parametrize("flag", ["0", "1"])
def test_removed_descriptive_signals_remain_errors(monkeypatch, signal, flag):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", flag)
    assert [v.severity for v in check_factual_claims(signal, _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize("prefix", ["không", "chẳng"])
@pytest.mark.parametrize("noun", ["đường", "corticoid", "chì", "BPA", "hóa chất"])
def test_negative_composition_claim_blocks_demotion(monkeypatch, prefix, noun):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thiết kế {prefix} {noun}"
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize("prefix", ["không", "chẳng"])
@pytest.mark.parametrize(
    "following", "thấy cần phải bị lo sợ mất tốn cam hề quá nhiều lâu khó ai gì còn chỉ".split()
)
def test_allowed_negative_following_words_do_not_block_demotion(monkeypatch, prefix, following):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thiết kế tiện lợi {prefix} {following}"
    # The negation guard is necessary but no longer sufficient: all tokens must
    # also be permitted by the descriptive vocabulary allowlist.
    severity = (
        Severity.WARNING
        if prefix == "không" and following in "thấy cần bị quá ai gì còn chỉ".split()
        else Severity.ERROR
    )
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [severity]


@pytest.mark.parametrize("prefix", ["không", "chẳng", "chưa", "không hề", "không phải"])
def test_benefit_with_negated_health_word_remains_error(monkeypatch, prefix):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thiết kế {prefix} mụn"
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize(
    "effect",
    [
        "da",
        "làn da",
        "tóc",
        "răng",
        "lỗ chân lông",
        "cơ thể",
        "cột sống",
        "xương",
        "trẻ hóa",
        "trẻ hoá",
        "thải độc",
        "nhạy cảm",
        "ngủ ngon",
        "collagen",
        "corticoid",
        "hormone",
        "cồn",
        "độc tố",
        "thuốc trừ sâu",
        "chì",
        "BPA",
        "giải nhiệt",
        "giải rượu",
        "cho người già",
        "cho người bệnh",
        "bé thông minh",
        "trẻ thông minh",
        "bổ máu",
        "bồi bổ",
        "bổ sung canxi",
        "phòng bệnh",
        "chứa hoạt chất",
    ],
)
def test_effect_words_block_otherwise_descriptive_clause(monkeypatch, effect):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thiết kế {effect.upper()}"
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]


@pytest.mark.parametrize(
    "unit",
    (
        "mah w kw wh v ml l lít g gr kg mg cm mm m inch gb tb mb hz mhz ghz mp dpi rpm "
        "viên gói miếng cái chiếc lượt vòng độ °c lumen lux ppi fps"
    ).split(),
)
def test_written_number_with_measurement_blocks_demotion(monkeypatch, unit):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    text = f"Thiết kế hai {unit.upper()}"
    assert [v.severity for v in check_factual_claims(text, _ctx())] == [Severity.ERROR]


def _gate() -> ScriptGate:
    segment, full = default_segment_rules(), default_full_script_rules()
    return ScriptGate(ScriptRuleRegistry([*segment.rules, *full.rules]), segment, full)


_OWNER_TEXT = "Giá chỉ 199.000đ, bảo hành 5 năm, thiết kế tiện lợi"


@pytest.mark.parametrize("flag", ["0", "1"])
def test_owner_full_script_claims_are_advisory_only_when_relaxed(monkeypatch, flag):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", flag)
    context = ScriptGateContext(facts=ProductFacts(prices=("299.000đ",)), total_min_seconds=0)
    result = _gate().run_full_script([_OWNER_TEXT], context)
    claims = [v for v in result.violations if v.rule_id.startswith("CLAIM_")]
    assert {v.rule_id for v in claims} >= {"CLAIM_PRICE", "CLAIM_FACTUAL"}
    assert all(v.segment_index == 0 for v in claims)
    assert result.passed == (flag == "1")
    if flag == "1":
        assert all(v.severity == Severity.WARNING for v in claims)
        price = next(v for v in claims if v.rule_id == "CLAIM_PRICE")
        original = check_price_claims(_OWNER_TEXT, context)[0]
        assert price.message == original.message
        assert price.text_span == original.text_span
    else:
        assert any(v.severity == Severity.ERROR for v in claims)


def test_owner_advisory_mode_does_not_relax_segment_errors(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    result = _gate().run_segment(_OWNER_TEXT, _ctx(prices=("299.000đ",)))
    assert not result.passed
    assert any(
        v.rule_id.startswith("CLAIM_") and v.severity == Severity.ERROR for v in result.violations
    )


@pytest.mark.parametrize(
    ("suffix", "rule_id"),
    [
        (" địt", "PROFANITY_OFFENSIVE"),
        ("\x01", "FORMAT_CONTROL"),
        (" <b>mua ngay</b>", "TTS_MARKUP"),
    ],
)
def test_owner_advisory_mode_preserves_non_claim_errors(monkeypatch, suffix, rule_id):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    context = ScriptGateContext(facts=ProductFacts(prices=("299.000đ",)), total_min_seconds=0)
    result = _gate().run_full_script([_OWNER_TEXT + suffix], context)
    assert not result.passed
    assert any(v.rule_id == rule_id and v.severity == Severity.ERROR for v in result.violations)
    assert all(
        v.severity == Severity.WARNING for v in result.violations if v.rule_id.startswith("CLAIM_")
    )


def test_cross_part_contradiction_still_blocks_in_relaxed_mode(monkeypatch):
    # run_full_script also gates AI-compiled scripts; a contradiction across parts cannot be
    # caught per segment, so it stays an error even when other claim findings are advisory.
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    result = _gate().run_full_script(
        ["Sản phẩm an toàn.", "Sản phẩm độc hại."],
        ScriptGateContext(total_min_seconds=0),
    )
    assert not result.passed
