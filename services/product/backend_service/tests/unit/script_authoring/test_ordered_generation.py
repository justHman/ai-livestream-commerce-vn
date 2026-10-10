"""Ordered session script generation (fake LLM, fake repos, real gate; no network)."""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from test_service_impl_generation import (
    _FakeEngineManager,
    _FakeRepos,
    ScriptAuthoringConfig,
    ScriptAuthoringServiceImpl,
)

from backend.application.clients.llm.openai_compatible import LLMClientError
from backend.application.script_authoring.generation.ordered_units import (
    BoundedLLM,
    LLMDeadlineError,
    ProductBrief,
    SessionBrief,
    UnitSpec,
    build_unit_prompt,
    check_unit_text,
    clean_unit_text,
    fallback_unit_text,
    plan_units,
    spoken_price,
)
from backend.application.script_authoring.models import ScriptState
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.duration import spoken_duration_ms
from backend.application.script_authoring.units import split_units

P2_CLAIMS = [
    "Kem dưỡng ẩm sâu cho tóc khô.",
    "Chất kem mềm mượt dễ thoa.",
    "Bảo hành 1 năm.",
    "Giảm 20% vào thứ Tư đầu tháng.",
]
P1_CLAIMS = [
    "Gel làm sạch nhẹ nhàng mỗi ngày.",
    "Chai nhỏ gọn dùng được lâu.",
    "Có size từ 36 đến 44.",
]
BRIEF = {
    "title": "Live thử nghiệm",
    "shop_name": "",  # unknown shop: the opening must not invent a name
    "product_facts": {
        "P2": {
            "product_name": "Kem ABC",
            "prices": ["100000.00 VND"],
            "allowed_claims": P2_CLAIMS,
            "claims_by_type": {
                "feature": P2_CLAIMS[:2],
                "warranty": [P2_CLAIMS[2]],
                "promotion": [P2_CLAIMS[3]],
            },
            "product_info": {"brand": "Hãng A", "category": "Mỹ phẩm"},
        },
        "P1": {
            "product_name": "Gel XYZ",
            "prices": ["150000.00 VND", "200000.00 VND"],
            "allowed_claims": P1_CLAIMS,  # no claims_by_type: clustered from the flat list
        },
    },
}


class FakeLLM:
    """Writes each unit from the facts its prompt carries; records every prompt."""

    def __init__(self, empty_for=None, raises=None):
        self.prompts = []
        self.empty_for = empty_for or set()
        self.raises = raises

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        title = re.search(r"\((\d+)/(\d+)\): ([^.]+)\.", prompt).group(3)
        name = re.search(r"Tên sản phẩm: (.+)", prompt).group(1)
        before_used = prompt.split("Đã nói ở các phần trước")[0]
        items = re.findall(r"^- (.+)$", before_used, flags=re.MULTILINE)
        if (name, title) in self.empty_for:
            return ""
        if title == "Mở đầu phiên live":
            return "Chào cả nhà, chào mừng mọi người đến với buổi live hôm nay, bạn nào cần hỏi size hay giá cứ bình luận cho mình nhé."
        if title == "Giới thiệu sản phẩm":
            lead = "Nào, chuyển sang món kế tiếp là " if "lời nối" in prompt else "Mình giới thiệu "
            return f"{lead}{name}, một sản phẩm rất dễ làm quen với cả nhà."
        if title == "Bảng size":
            return "Có nhiều size từ nhỏ đến lớn, mọi người nhắn chiều cao cân nặng để mình tư vấn size nhé."
        if title in ("Điểm nổi bật", "Cam kết và lưu ý"):
            return " ".join(items)
        if title == "Giá và ưu đãi":
            prices = re.search(r"đã đọc thành chữ\): (.+)", prompt).group(1)
            soft = " Bạn nào quan tâm thì nhắn mình nhé." if "lời mời nhẹ" in prompt else ""
            return f"Giá chỉ {prices}. {' '.join(items)}{soft}"
        return (
            "Cảm ơn mọi người đã theo dõi suốt buổi, hẹn gặp lại cả nhà ở những buổi live sau nhé."
        )


def _service(llm, **config):
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos,
        config=ScriptAuthoringConfig(**config),
        gate=ScriptAuthoringServiceImpl._default_gate(),
        engine_manager=_FakeEngineManager(llm_fn=llm),
    )
    return service, repos


async def _new_set(service, policy="ORDER_AWARE"):
    # owner order P2, P1 is the OPPOSITE of the lexical id order
    return (
        await service.create_script_set(
            name="Phiên", transition_policy=policy, product_ids=["P2", "P1"], brief=BRIEF
        )
    )["id"]


async def _run(service, repos, set_id, key="k1"):
    started = await service.start_batch_generation(
        set_id=set_id,
        product_ids=[],
        target_duration_s=600,
        idempotency_key=key,
        ordered_units=True,
    )
    for _ in range(600):
        _batch, state = repos.batches.rows[started["batch_id"]]
        if state.status in ("completed", "partial_completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.02)
    return started["batch_id"]


async def _text(repos, set_id, pid):
    item = await repos.items.get_by_product(set_id, pid)
    version = await repos.versions.get(item.current_version_id)
    return item, version


@pytest.mark.asyncio
async def test_units_adapt_to_the_facts_and_never_repeat_a_fact() -> None:
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)

    first, v_first = await _text(repos, set_id, "P2")
    last, v_last = await _text(repos, set_id, "P1")
    assert first.state is ScriptState.REVIEWABLE and last.state is ScriptState.REVIEWABLE
    u2, u1 = split_units(v_first.spoken_text), split_units(v_last.spoken_text)
    # P2: opening, intro, features, warranty, offer(+promotion); P1: intro, ONE claim unit, offer, closing
    assert len(u2) == 5 and len(u1) == 4
    assert len(llm.prompts) == 9  # one call per unit, no planning call, no retries
    # every approved claim is spoken exactly once per product
    for claim in P2_CLAIMS:
        assert v_first.spoken_text.count(claim) == 1
    for claim in P1_CLAIMS:
        assert v_last.spoken_text.count(claim) == 1
    # the first product never says "next product"; the bridge lives only in the later intro
    assert "tiếp theo" not in u2[1] and "kế tiếp" not in u2[1]
    assert "kế tiếp là Gel XYZ" in u1[0]
    assert "Gel XYZ" not in v_first.spoken_text  # nothing bridges at the END of the previous one
    # prices are spoken words, never machine text
    assert "một trăm nghìn đồng" in u2[4] and "hai trăm nghìn đồng" in u1[2]
    full = v_first.spoken_text + v_last.spoken_text
    assert not re.search(r"\d+[.,]\d\d|VND", full)
    # conditional promotion keeps its condition and is not hard-sold
    assert "vào thứ Tư đầu tháng" in u2[4]
    assert not re.search(r"ngay|bỏ lỡ|chốt đơn", full)
    # warm-up opening invites comments; no shop name is invented
    assert "bình luận" in u2[0]
    opening_prompt = next(p for p in llm.prompts if "Mở đầu phiên live" in p)
    assert "Chưa biết tên shop" in opening_prompt
    # exactly one soft CTA per product, in its last unit
    assert sum("lời mời nhẹ" in p for p in llm.prompts) == 2

    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    assert snapshot["outcome"] == "succeeded" and snapshot["total"] == 2 and snapshot["done"] == 2


@pytest.mark.asyncio
async def test_three_claims_make_at_most_four_units_with_intro_and_offer() -> None:
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    _item, version = await _text(repos, set_id, "P1")
    assert len(split_units(version.spoken_text)) <= 4  # intro, claims, offer, (closing)


@pytest.mark.asyncio
async def test_order_agnostic_set_never_bridges_to_neighbours() -> None:
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service, policy="ORDER_AGNOSTIC")
    await _run(service, repos, set_id)
    assert not any("lời nối" in p or "sản phẩm trước (" in p for p in llm.prompts)


@pytest.mark.asyncio
async def test_failed_unit_becomes_an_extractive_fallback_never_a_placeholder(caplog) -> None:
    llm = FakeLLM(empty_for={("Gel XYZ", "Giá và ưu đãi")})
    service, repos = _service(llm)
    set_id = await _new_set(service)
    with caplog.at_level("WARNING"):
        batch_id = await _run(service, repos, set_id)

    item, version = await _text(repos, set_id, "P1")
    units = split_units(version.spoken_text)
    assert item.state is ScriptState.REVIEWABLE and len(units) == 4
    assert units[2] == "Giá một trăm năm mươi nghìn đồng hoặc hai trăm nghìn đồng."
    assert "<" not in version.spoken_text and ">" not in version.spoken_text
    assert units[1] == " ".join(P1_CLAIMS)  # the other units are untouched
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    statuses = {p["product_id"]: p for p in snapshot["products"]}
    assert snapshot["outcome"] == "succeeded" and statuses["P1"]["status"] == "done"
    assert statuses["P1"]["fallback_units"] == [{"unit_index": 2, "reasons": ["guard:empty"]}]
    assert "fallback_units" not in statuses["P2"]
    # every failed attempt is logged with product, unit and the machine reason
    assert "product=P1 unit=2 role=offer attempt=1 reason=empty" in caplog.text


@pytest.mark.asyncio
async def test_provider_errors_are_bounded_and_fall_back_without_crashing(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    llm = FakeLLM(raises=LLMClientError("boom"))
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    assert snapshot["outcome"] == "succeeded"  # fallback text, flagged for review
    item, version = await _text(repos, set_id, "P2")
    assert item.state is ScriptState.REVIEWABLE and "<" not in version.spoken_text
    entry = next(p for p in snapshot["products"] if p["product_id"] == "P2")
    assert len(entry["fallback_units"]) == 5
    assert entry["fallback_units"][0]["reasons"] == ["llm_failed:LLMClientError"]
    assert len(llm.prompts) <= 9 * 2  # <= 2 transport attempts per unit


class TruncatingLLM(FakeLLM):
    """Cuts the highlight reply of Kem ABC mid-word; optionally only the first time."""

    def __init__(self, always=False):
        super().__init__()
        self.always = always
        self.cuts = 0

    def __call__(self, prompt):
        text = super().__call__(prompt)
        if "Điểm nổi bật" in prompt and "Kem ABC" in prompt and (self.always or not self.cuts):
            self.cuts += 1
            return text[:-12]  # ends mid-word, no sentence punctuation
        return text


@pytest.mark.asyncio
async def test_truncated_output_is_detected_and_retried_shorter() -> None:
    llm = TruncatingLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    _item, version = await _text(repos, set_id, "P2")
    assert P2_CLAIMS[0] in version.spoken_text and P2_CLAIMS[1] in version.spoken_text
    assert len(llm.prompts) == 10  # exactly one retry
    assert "NGẮN HƠN" in next(p for p in llm.prompts if p.endswith("kết thúc trọn câu."))
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    assert all("fallback_units" not in p for p in snapshot["products"])


@pytest.mark.asyncio
async def test_persistently_truncated_output_falls_back_with_its_reason() -> None:
    service, repos = _service(TruncatingLLM(always=True))
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    _item, version = await _text(repos, set_id, "P2")
    assert split_units(version.spoken_text)[2] == " ".join(P2_CLAIMS[:2])  # verbatim claims
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    entry = next(p for p in snapshot["products"] if p["product_id"] == "P2")
    assert entry["fallback_units"] == [{"unit_index": 2, "reasons": ["guard:truncated"]}]


@pytest.mark.asyncio
async def test_existing_owner_text_is_never_overwritten() -> None:
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await service.save_draft(
        set_id=set_id,
        product_id="P2",
        display_text="Bài của chủ shop.",
        spoken_text="Bài của chủ shop.",
        revision=None,
    )
    await _run(service, repos, set_id)
    _item, version = await _text(repos, set_id, "P2")
    assert version.spoken_text == "Bài của chủ shop."
    assert not any("Tên sản phẩm: Kem ABC" in p for p in llm.prompts)
    assert (await _text(repos, set_id, "P1"))[0].state is ScriptState.REVIEWABLE


@pytest.mark.asyncio
async def test_idempotency_key_rejects_a_different_request_and_foreign_batches() -> None:
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    other_set = await _new_set(service)
    batch_id = await _run(service, repos, set_id, key="same")
    again = await service.start_batch_generation(
        set_id=set_id,
        product_ids=[],
        target_duration_s=600,
        idempotency_key="same",
        ordered_units=True,
    )
    assert again["batch_id"] == batch_id and again["idempotent"] is True
    with pytest.raises(ScriptAuthoringError) as conflict:
        await service.start_batch_generation(
            set_id=set_id,
            product_ids=["P1"],
            target_duration_s=600,
            idempotency_key="same",
            ordered_units=True,
        )
    assert conflict.value.code == "idempotency_conflict"
    for call in (service.get_batch, service.cancel_batch):
        with pytest.raises(ScriptAuthoringError) as foreign:
            await call(set_id=other_set, batch_id=batch_id)
        assert foreign.value.code == "not_found"
    assert await service.get_batch_events_snapshot(set_id=other_set, batch_id=batch_id) is None


def test_plan_clusters_flat_claims_by_keyword_and_uses_each_once() -> None:
    brief = ProductBrief(
        product_id="p",
        name="N",
        prices=("1000 VND",),
        claims=(
            "Chất liệu cotton.",
            "Form rộng thoải mái.",
            "Bảo hành 6 tháng.",
            "Đổi trả trong 7 ngày.",
            "Tặng túi vải khi mua hai.",
            "Chất liệu cotton.",
        ),
    )
    units = plan_units(brief, first=True, last=True)
    assert [u.role for u in units] == [
        "opening",
        "intro",
        "highlight",
        "assurance",
        "offer",
        "closing",
    ]
    spoken = [c for u in units for c in (*u.claims, *u.promos)]
    assert sorted(spoken) == sorted(set(brief.claims))  # once each, duplicates folded
    assert units[-2].with_cta and not any(u.with_cta for u in units[:-2])
    assert units[-2].promos == ("Tặng túi vải khi mua hai.",)


def test_plan_without_facts_is_just_intro_with_the_cta() -> None:
    units = plan_units(ProductBrief(product_id="p", name="N"), first=False, last=False)
    assert [u.role for u in units] == ["intro"] and units[0].with_cta


def test_plan_bridge_only_for_a_later_product_on_a_locked_order() -> None:
    brief = ProductBrief(product_id="p", name="N")
    assert plan_units(brief, first=False, last=False, ordered_aware=True)[0].bridge
    assert not plan_units(brief, first=True, last=False, ordered_aware=True)[0].bridge
    assert not plan_units(brief, first=False, last=False, ordered_aware=False)[0].bridge


def test_spoken_price_words() -> None:
    assert spoken_price("100000.00 VND") == "một trăm nghìn đồng"
    assert spoken_price("299.000đ") == "hai trăm chín mươi chín nghìn đồng"
    assert spoken_price("2,5 triệu") == "hai triệu năm trăm nghìn đồng"
    assert spoken_price("liên hệ") is None


def test_clean_unit_text_rules() -> None:
    assert clean_unit_text("") is None
    assert clean_unit_text("...") is None
    assert clean_unit_text("x" * 800) is None
    assert clean_unit_text('"Giá tốt — mời bạn xem nhé."') == "Giá tốt, mời bạn xem nhé."
    assert clean_unit_text("- Một ý.\n\nVà ý nữa.") == "Một ý. Và ý nữa."
    # never mention missing data; never hard-sell
    assert clean_unit_text("Hiện chưa có thông tin khuyến mãi nhé cả nhà.") is None
    assert clean_unit_text("Đặt hàng ngay để không bỏ lỡ nhé cả nhà.") is None
    # a pointer to "the next product" only where a bridge belongs
    assert clean_unit_text("Sau đó là sản phẩm tiếp theo nhé cả nhà.") is None
    assert clean_unit_text("Kế tiếp là Gel XYZ nhé cả nhà.", allow_bridge=True)
    # approved prices become words, an unapproved price stays as digits for the gate
    assert (
        clean_unit_text("Giá 100000.00 VND thôi nhé.", prices=("100000.00 VND",))
        == "Giá một trăm nghìn đồng thôi nhé."
    )
    assert "999.000" in clean_unit_text("Giá 999.000đ thôi nhé.", prices=("100000.00 VND",))
    # sizes stay one token
    assert clean_unit_text("Có size X L và X X L nhé cả nhà.") == "Có size XL và XXL nhé cả nhà."


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("Quần jean có cân nặng sáu mươi lăm đến bảy mươ", "truncated"),
        ("Mình tư vấn cho quý vị nhé.", "address"),
        ("Mời anh chị xem nhé.", "address"),
        ("Có size M L XL và không phơi ngoài nắng nhé.", "mashup"),
        ("Hiện chưa có thông tin về mẫu này nhé.", "missing_info"),
    ],
)
def test_unit_guards_report_a_machine_reason(raw, reason) -> None:
    assert check_unit_text(raw) == (None, reason)


def test_one_topic_per_sentence_and_typos_are_fixed() -> None:
    assert check_unit_text("Có đủ size từ M đến XL. Nên giặt nhẹ và không phơi nắng.")[0]
    assert (
        check_unit_text("Mọi người thoải chọn size nhé.")[0] == "Mọi người thoải mái chọn size nhé."
    )


def test_the_full_product_name_is_not_repeated_in_every_unit() -> None:
    mid = UnitSpec("highlight")
    kwargs = {"spec": mid, "product_name": "Áo khoác denim"}
    text = "Áo khoác denim này rất bền nhé."
    assert check_unit_text(text, name_used_elsewhere=2, **kwargs)[0]
    assert check_unit_text(text, name_used_elsewhere=3, **kwargs) == (None, "name_repeat")
    intro = UnitSpec("intro")
    assert check_unit_text(text, spec=intro, product_name="Áo khoác denim", name_used_elsewhere=5)[
        0
    ]


def test_only_the_last_unit_may_carry_a_call_to_action() -> None:
    cta = "Mọi người nhắn mình để đặt hàng nhé."
    assert check_unit_text(cta, spec=UnitSpec("highlight")) == (None, "cta")
    assert check_unit_text(cta, spec=UnitSpec("offer", with_cta=True))[0]
    assert check_unit_text(cta, spec=UnitSpec("closing"))[0]


def test_typed_claims_cap_only_decorative_ones_and_count_the_rest() -> None:
    rows = [
        f"Cao 1m{50 + i} đến 1m{52 + i}, nặng {45 + i} đến {48 + i} ký: size M." for i in range(12)
    ]
    features = [
        f"Điểm nổi bật số {w} của mẫu này."
        for w in "a b c d e f g h i j k l m n o p q r s t".split()
    ]
    policy = [f"Chính sách số {w}: đổi trả dễ." for w in "một hai ba bốn năm sáu bảy tám".split()]
    brief = ProductBrief(
        product_id="p",
        name="Áo khoác denim",
        prices=("100000.00 VND",),
        claims=tuple(features + rows + policy),
        claims_by_type=(("feature", tuple(features + rows)), ("shipping", tuple(policy))),
    )
    units = plan_units(brief, first=False, last=False)
    roles = [u.role for u in units]
    assert roles == [
        "intro",
        "highlight",
        "highlight",
        "highlight",
        "sizes",
        "assurance",
        "assurance",
        "offer",
    ]
    by_role = {u.role: u for u in units}
    assert by_role["sizes"].claims == tuple(rows)  # the whole chart is ONE unit
    assert sum(len(u.claims) for u in units if u.role == "highlight") == 18  # decorative: capped
    shipping = [c for u in units if u.role == "assurance" for c in u.claims]
    assert sorted(shipping) == sorted(policy)  # must-keep type: all kept, <= 4 per unit
    assert all(len(u.claims) <= 4 for u in units if u.role == "assurance")
    assert [u.with_cta for u in units] == [False] * 7 + [True]


def test_untyped_claims_are_never_capped() -> None:
    claims = tuple(f"Ý số {i} của mẫu này." for i in range(10))
    units = plan_units(
        ProductBrief(product_id="p", name="Áo", claims=claims), first=False, last=False
    )
    spoken = [c for u in units for c in u.claims]
    assert sorted(spoken) == sorted(claims) and all(len(u.claims) <= 4 for u in units)


def test_typed_limitation_survives_with_many_features() -> None:
    features = tuple(f"Tính năng số {i} của mẫu này." for i in range(9))
    restriction = "Chỉ áp dụng cho lỗi sản xuất."
    brief = ProductBrief(
        product_id="p",
        name="Áo",
        claims=(*features, "Bảo hành một năm.", restriction),
        claims_by_type=(
            ("feature", features),
            ("warranty", ("Bảo hành một năm.",)),
            ("limitation", (restriction,)),
        ),
    )
    units = plan_units(brief, first=False, last=False)
    warranty_unit = next(u for u in units if u.role == "assurance")
    assert warranty_unit.claims == ("Bảo hành một năm.", restriction)  # same unit, warranty first
    assert restriction in warranty_unit.must
    assert len(next(u for u in units if u.role == "highlight").claims) == 6


def test_32_limitations_become_short_units_within_the_duration_bound() -> None:
    claims = tuple(
        f"Không dùng cho trường hợp số {i} vì có thể gây kích ứng da nhạy cảm của người dùng."
        for i in range(32)
    )
    brief = ProductBrief(
        product_id="p",
        name="Kem",
        claims=claims,
        claims_by_type=(("limitation", claims),),
    )
    units = [u for u in plan_units(brief, first=False, last=False) if u.role == "assurance"]
    assert len(units) == 8 and all(len(u.claims) == 4 for u in units)
    for unit in units:
        for paragraph in fallback_unit_text(unit, brief).split("\n\n"):
            assert spoken_duration_ms(paragraph) / 1000.0 <= 45.0
            assert paragraph.count("Không dùng") <= 3


def test_size_chart_fallback_always_speaks_a_restricted_row() -> None:
    rows = [
        f"Cao 1m{50 + i} đến 1m{52 + i}, nặng {45 + i} đến {48 + i} ký: size M." for i in range(30)
    ]
    rows[2] = "Cao 1m60 đến 1m62, nặng 55 đến 58 ký: size L, không giặt máy."
    brief = ProductBrief(product_id="p", name="Quần", claims=tuple(rows))
    sizes = next(u for u in plan_units(brief, first=False, last=False) if u.role == "sizes")
    assert rows[2] in sizes.must
    text = fallback_unit_text(sizes, brief)
    assert "không giặt máy" in text
    for paragraph in text.split("\n\n"):
        assert spoken_duration_ms(paragraph) / 1000.0 <= 45.0


def test_numeric_comparisons_are_not_markup() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text
    from backend.application.script_authoring.gate.rules.tts_readiness import check_tts_markup

    text = "Bảo quản ở nhiệt độ <5 hoặc >40 thì không tốt."
    assert "dưới năm hoặc trên bốn mươi" in compile_spoken_text(text).spoken_text
    assert check_tts_markup(text, None) == []
    assert check_tts_markup("Có <b>chữ</b> đậm.", None)  # real tags still flagged


def test_legacy_unlocked_order_gets_no_first_last_wording() -> None:
    brief = ProductBrief(product_id="p", name="Áo", claims=("Áo cotton.",))
    units = plan_units(brief, first=True, last=True, ordered_aware=False)
    assert not any(u.first_product or u.last_product for u in units)
    text = "Đây là sản phẩm cuối cùng của buổi live nhé."
    assert check_unit_text(text, spec=UnitSpec("highlight"), aware=False) == (None, "position")
    assert check_unit_text(text, spec=UnitSpec("closing"), aware=False)[0]
    assert check_unit_text(text, spec=UnitSpec("highlight"), aware=True)[0]


@pytest.mark.parametrize("claim_text", ["Do not use during pregnancy."])
def test_claim_coverage_is_lexical_and_numbers_are_exact(claim_text) -> None:
    from backend.application.script_authoring.generation.ordered_units import claim_covered

    assert claim_covered("Bảo hành 1 năm.", "Mọi người yên tâm, bảo hành một năm nhé.")
    assert not claim_covered("Bảo hành 1 năm.", "Bảo hành hai năm nhé.")
    assert claim_covered("Chỉ áp dụng cho lỗi sản xuất.", "Chỉ áp dụng cho lỗi sản xuất nhé.")
    assert not claim_covered(claim_text, "Không dùng trong thời gian mang thai nhé.")


class RestrictionDroppingLLM(FakeLLM):
    """Writes only the first claim of an assurance unit and translates the English one."""

    def __call__(self, prompt):
        text = super().__call__(prompt)
        if "Cam kết và lưu ý" in prompt:
            self.prompts.append("assurance-call")
            return text.split(". ")[0].rstrip(".") + "."
        return text


@pytest.mark.asyncio
async def test_missing_qualifier_falls_back_to_verbatim_claims_after_one_retry() -> None:
    claims = ["Bảo hành một năm.", "Chỉ áp dụng cho lỗi sản xuất.", "Do not use during pregnancy."]
    brief = {
        **BRIEF,
        "product_facts": {
            "P2": {
                "product_name": "Kem ABC",
                "prices": ["100000.00 VND"],
                "allowed_claims": claims,
                "claims_by_type": {
                    "warranty": [claims[0]],
                    "limitation": [claims[1]],
                    "compliance": [claims[2]],
                },
            },
            "P1": BRIEF["product_facts"]["P1"],
        },
    }
    llm = RestrictionDroppingLLM()
    service, repos = _service(llm)
    set_id = (
        await service.create_script_set(
            name="X", transition_policy="ORDER_AWARE", product_ids=["P2", "P1"], brief=brief
        )
    )["id"]
    batch_id = await _run(service, repos, set_id)
    _item, version = await _text(repos, set_id, "P2")
    for claim in claims:
        assert claim in version.spoken_text  # every restriction spoken, verbatim
    assert sum("BẮT BUỘC" in p for p in llm.prompts) == 1  # exactly one retry
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    entry = next(p for p in snapshot["products"] if p["product_id"] == "P2")
    assert entry["fallback_units"][0]["reasons"] == ["guard:coverage"]
    assert "claims_not_spoken" not in entry


def test_fallback_units_use_only_approved_words_and_no_markup() -> None:
    brief = ProductBrief(
        product_id="p",
        name="Gel XYZ",
        prices=("150000.00 VND",),
        claims=("Gel làm sạch nhẹ.", "Chai nhỏ gọn.", "Dùng được lâu.", "Mùi dễ chịu."),
    )
    highlight = fallback_unit_text(UnitSpec("highlight", claims=tuple(brief.claims)), brief)
    # ALL claims, verbatim, in short paragraphs (never truncated to the first few)
    assert highlight == "Gel làm sạch nhẹ. Chai nhỏ gọn. Dùng được lâu.\n\nMùi dễ chịu."
    offer = fallback_unit_text(UnitSpec("offer", promos=("Tặng quà nhỏ.",), with_price=True), brief)
    assert offer == "Giá một trăm năm mươi nghìn đồng. Tặng quà nhỏ."
    for role in ("opening", "intro", "closing", "assurance", "sizes", "offer"):
        text = fallback_unit_text(UnitSpec(role), brief)
        assert text and "<" not in text and ">" not in text


def test_bounded_llm_retries_once_then_stops_and_honours_deadline() -> None:
    calls = []

    def flaky(prompt):
        calls.append(prompt)
        raise ConnectionError("down")

    bounded = BoundedLLM(flaky, attempts=2, sleep=lambda _s: None)
    with pytest.raises(ConnectionError):
        bounded("p")
    assert len(calls) == 2

    now = [0.0]
    late = BoundedLLM(flaky, job_deadline_s=10, clock=lambda: now[0], sleep=lambda _s: None)
    now[0] = 11.0
    with pytest.raises(LLMDeadlineError):
        late("p")
    assert len(calls) == 2  # the expired job made no further call


def test_short_one_sentence_units_pass_role_bounds_and_common_phrases_only_warn() -> None:
    from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
    from backend.application.script_authoring.generation.ordered_units import role_bounds_s

    gate = ScriptAuthoringServiceImpl._default_gate()
    low, high = role_bounds_s("offer")
    ctx = ScriptGateContext(facts=ProductFacts(), target_min_seconds=low, target_max_seconds=high)
    assert gate.run_segment("Mời bạn xem giỏ hàng nhé.", ctx).passed
    # a phrase shared by 6 of 7 units is a warning, not a block
    units = [f"Cả nhà ơi mình nói ý số {w} nhé." for w in "một hai ba bốn năm sáu bảy".split()]
    full = gate.run_full_script(units, ScriptGateContext(facts=ProductFacts(), total_min_seconds=1))
    assert full.passed


@pytest.mark.asyncio
async def test_regeneration_gates_with_the_real_product_facts() -> None:
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    # the owner edits P1 by hand (DRAFT) and then regenerates its offer unit
    await service.save_draft(
        set_id=set_id,
        product_id="P1",
        display_text="Bản nháp.",
        spoken_text="Bản nháp.",
        revision=None,
    )
    service._engine_manager._llm_fn = lambda _p: (
        "Gel XYZ đang có giá 150k thôi, bạn nào quan tâm thì nhắn mình một cách thoải mái nhé."
    )
    await service.regenerate_segment(
        set_id=set_id, product_id="P1", segment_index=2, idempotency_key="regen"
    )
    for _ in range(300):
        item, version = await _text(repos, set_id, "P1")
        if "150k" in version.spoken_text:
            break
        await asyncio.sleep(0.02)
    assert "150k" in version.spoken_text
    assert item.state is ScriptState.REVIEWABLE


@pytest.mark.asyncio
async def test_manual_draft_without_spoken_text_compiles_prices_and_keeps_units() -> None:
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    display = "Giá 100000.00 VND hôm nay.\n\nCó size XL và XXL."
    await service.save_draft(
        set_id=set_id, product_id="P2", display_text=display, spoken_text=None, revision=None
    )
    _item, version = await _text(repos, set_id, "P2")
    assert version.display_text == display
    assert split_units(version.spoken_text) == (
        "Giá một trăm nghìn đồng hôm nay.",
        "Có size XL và XXL.",
    )


@pytest.mark.asyncio
async def test_recovery_cannot_extend_the_whole_job_deadline() -> None:
    service, repos = _service(FakeLLM(), unit_job_deadline_s=100.0)
    set_id = await _new_set(service)
    script_set = await repos.script_sets.get(set_id)
    started = time.time() - 1000  # the job started long ago: nothing is left
    ordered = service._ordered_batch(script_set, lambda _p: "ok", started_at=started)
    with pytest.raises(LLMDeadlineError):
        ordered.llm("p")
    fresh = service._ordered_batch(script_set, lambda _p: "ok", started_at=None)
    assert fresh.llm("p") == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", "\n "])
async def test_blank_spoken_text_means_not_provided_and_is_compiled(blank) -> None:
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await service.save_draft(
        set_id=set_id,
        product_id="P2",
        display_text="Giá 299.000đ nhé.\n\nSize XL.",
        spoken_text=blank,
        revision=None,
    )
    _item, version = await _text(repos, set_id, "P2")
    assert split_units(version.spoken_text) == (
        "Giá hai trăm chín mươi chín nghìn đồng nhé.",
        "Size XL.",
    )


@pytest.mark.asyncio
async def test_non_blank_spoken_text_still_wins_verbatim() -> None:
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await service.save_draft(
        set_id=set_id,
        product_id="P2",
        display_text="Giá 299.000đ",
        spoken_text="Giá chốt riêng của chủ shop.",
        revision=None,
    )
    assert (await _text(repos, set_id, "P2"))[1].spoken_text == "Giá chốt riêng của chủ shop."


def test_typed_claims_never_add_facts_beyond_the_flat_list() -> None:
    brief = ProductBrief(
        product_id="p",
        name="N",
        claims=("Chất liệu cotton.",),
        claims_by_type=(("feature", ("Chất liệu cotton.", "Có size XL.")),),
    )
    units = plan_units(brief, first=False, last=False)
    spoken = [c for u in units for c in (*u.claims, *u.promos)]
    assert spoken == ["Chất liệu cotton."]


def test_crlf_blank_lines_are_unit_boundaries() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    text = "Đoạn đầu.\r\n\r\nĐoạn sau."
    assert split_units(text) == ("Đoạn đầu.", "Đoạn sau.")
    assert split_units(compile_spoken_text(text).spoken_text) == ("Đoạn đầu.", "Đoạn sau.")


def test_missing_data_phrase_allowed_only_when_an_approved_statement_says_it() -> None:
    sentence = "Lưu ý là không có khuyến mãi cho đơn dưới 200k nhé cả nhà."
    approved = ("Không có khuyến mãi cho đơn dưới 200k.",)
    assert clean_unit_text(sentence) is None
    assert clean_unit_text(sentence, approved=approved) is not None
    assert clean_unit_text("Hiện chưa có khuyến mãi nhé cả nhà.", approved=approved) is None


def test_missing_data_phrase_exception_ignores_whitespace_differences() -> None:
    approved = ("Không có  khuyến mãi cho đơn dưới 200k.\nTheo từng đợt.",)
    sentence = "Lưu ý là không có khuyến mãi cho đơn dưới 200k nhé cả nhà."
    assert clean_unit_text(sentence, approved=approved) is not None


def test_restrictions_are_never_dropped_by_a_cap_or_a_fallback() -> None:
    claims = (
        "Bảo hành một năm.",
        "Đổi trả trong bảy ngày.",
        "Giao hàng toàn quốc.",
        "Không bảo hành khi giặt máy.",
    )
    brief = ProductBrief(product_id="p", name="Áo", claims=claims)
    spec = next(u for u in plan_units(brief, first=False, last=False) if u.role == "assurance")
    assert spec.claims[2] == "Không bảo hành khi giặt máy."  # after its warranty claims
    text = fallback_unit_text(spec, brief)
    assert all(c in text for c in claims)  # ALL claims, verbatim
    many = tuple(f"Cam kết số {i}." for i in range(20)) + claims
    units = plan_units(ProductBrief("p", "Áo", claims=many), first=False, last=False)
    spoken = [c for u in units if u.role == "assurance" for c in u.claims]
    assert "Không bảo hành khi giặt máy." in spoken and len(spoken) == len(many)


def test_markdown_in_approved_claims_is_unwrapped_never_deleted() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    text = "Bảo hành một năm. **Không bảo hành khi giặt máy.** _Giao nhanh_ và `đổi trả`."
    assert compile_spoken_text(text).spoken_text == (
        "Bảo hành một năm. Không bảo hành khi giặt máy. Giao nhanh và đổi trả."
    )
    brief = ProductBrief(product_id="p", name="Áo", claims=(text,))
    out = fallback_unit_text(UnitSpec("assurance", claims=(text,)), brief)
    assert "Không bảo hành khi giặt máy." in out


def test_unparseable_price_never_invents_an_offer() -> None:
    brief = ProductBrief(
        product_id="p", name="Áo", prices=("Liên hệ shop",), claims=("Áo cotton.",)
    )
    units = plan_units(brief, first=False, last=False)
    assert [u.role for u in units] == ["intro", "highlight"] and units[-1].with_cta
    offer = UnitSpec("offer")
    text = fallback_unit_text(offer, brief)
    assert not re.search(r"ưu đãi|khuyến mãi|giảm", text)
    promo = ProductBrief(product_id="p", name="Áo", claims=("Giảm 10% khi mua hai.",))
    assert [u.role for u in plan_units(promo, first=False, last=False)] == ["intro", "offer"]


@pytest.mark.parametrize(
    "sentence",
    [
        "Sản phẩm dành cho trẻ em.",
        "Áo co giãn và giữ form tốt.",
        "Áo vải cotton và có thể giặt máy.",
    ],
)
def test_ordinary_descriptions_pass_the_guards(sentence) -> None:
    assert check_unit_text(sentence, spec=UnitSpec("highlight")) == (sentence, None)


@pytest.mark.asyncio
async def test_batch_counts_exactly_the_claims_the_saved_text_does_not_speak() -> None:
    service, repos = _service(FakeLLM())
    features = [f"Điểm nổi bật số {i}." for i in range(20)]
    many = {
        "product_name": "Áo ABC",
        "prices": ["100000.00 VND"],
        "allowed_claims": features,
        "claims_by_type": {"feature": features},
    }
    brief = {**BRIEF, "product_facts": {"P2": many, "P1": BRIEF["product_facts"]["P1"]}}
    set_id = (
        await service.create_script_set(
            name="X", transition_policy="ORDER_AWARE", product_ids=["P2", "P1"], brief=brief
        )
    )["id"]
    batch_id = await _run(service, repos, set_id)
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    entry = {p["product_id"]: p for p in snapshot["products"]}
    assert entry["P2"]["claims_not_spoken"] == 2  # 18 of 20 decorative claims were spoken
    assert "claims_not_spoken" not in entry["P1"]


@pytest.mark.parametrize(
    "claim, text, covered",
    [
        ("Bảo hành 1 năm.", "Bảo hành một năm nhé.", True),
        ("Bảo hành 1 năm.", "Bảo hành hai mươi mốt năm nhé.", False),
        ("Bảo hành 1 năm.", "Bảo hành hai mươi một năm nhé.", False),
        ("Bảo hành 1 năm.", "Bảo hành 1 năm nhé.", True),
        ("Đổi trả trong 5 ngày.", "Đổi trả trong năm ngày.", True),
        ("Đổi trả trong 15 ngày.", "Đổi trả trong mười lăm ngày nhé.", True),
        ("Đổi trả trong 15 ngày.", "Đổi trả trong năm ngày nhé.", False),
        ("Đổi trả trong 5 ngày.", "Đổi trả trong mười lăm ngày nhé.", False),
        ("Giao trong 30 ngày.", "Giao trong ba ngày nhé.", False),
        ("Giao trong 3 ngày.", "Giao trong ba mươi ngày nhé.", False),
        ("Giao trong 105 ngày.", "Giao trong một trăm lẻ năm ngày.", True),
        ("Giảm 20% khi mua hàng.", "Giảm hai mươi phần trăm khi mua hàng.", True),
        ("Giảm 20% khi mua hàng.", "Giảm hai phần trăm khi mua hàng.", False),
        ("Dung tích 1,25 lít.", "Dung tích một phẩy hai mươi lăm lít.", True),
        ("Dung tích 1,25 lít.", "Dung tích 1,25 lít.", True),
        ("Cao 36-44.", "Cao từ 36 đến 44.", True),
        ("Cao 36-44.", "Cao từ ba mươi sáu đến bốn mươi bốn.", True),
        ("Cao 36-44.", "Cao từ ba mươi sáu đến bốn mươi.", False),
        ("Giá 105 nghìn.", "Giá một trăm lẻ năm nghìn.", True),
    ],
)
def test_number_coverage_uses_the_canonical_spoken_form(claim, text, covered) -> None:
    from backend.application.script_authoring.generation.ordered_units import claim_covered

    assert claim_covered(claim, text) is covered


def test_a_second_claim_in_the_same_unit_may_share_a_unit_word() -> None:
    from backend.application.script_authoring.generation.ordered_units import claim_covered

    both = ("Bảo hành 1 năm.", "Hạn dùng 2 năm.")
    text = "Bảo hành một năm, hạn dùng hai năm nhé."
    assert claim_covered(both[0], text, both) and claim_covered(both[1], text, both)


def test_restricted_size_rows_beyond_the_row_cap_are_never_dropped() -> None:
    rows = [
        f"Cao 1m{50 + i} đến 1m{52 + i}, nặng {45 + i} đến {48 + i} ký: size M." for i in range(30)
    ]
    restricted = [f"Cao 2m{i:02d}, nặng 9{i % 10} ký: size XL, không giặt máy." for i in range(45)]
    brief = ProductBrief(product_id="p", name="Quần", claims=tuple(rows + restricted))
    units = [u for u in plan_units(brief, first=False, last=False) if u.role == "sizes"]
    assert len(units) == 2 and all(len(u.claims) <= 40 for u in units)
    spoken = {c for u in units for c in u.claims}
    assert set(restricted) <= spoken  # every restricted row reaches a unit (and the fallback)
    assert len(spoken & set(rows)) < 30  # plain overflow rows are only counted, not forced
    text = "\n\n".join(fallback_unit_text(u, brief) for u in units)
    assert text.count("không giặt máy") == 45


def test_one_long_claim_is_split_at_clauses_within_the_bound_without_changing_words() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    clause = "Sản phẩm không dùng được trong trường hợp số {i} vì có thể gây kích ứng nặng"
    claim = ", ".join(clause.format(i=i) for i in range(14)) + "."
    assert spoken_duration_ms(compile_spoken_text(claim).spoken_text) / 1000.0 > 45.0
    brief = ProductBrief(
        product_id="p", name="Kem", claims=(claim,), claims_by_type=(("limitation", (claim,)),)
    )
    spec = next(u for u in plan_units(brief, first=False, last=False) if u.role == "assurance")
    paragraphs = fallback_unit_text(spec, brief).split("\n\n")
    assert len(paragraphs) > 1
    assert all(spoken_duration_ms(p) / 1000.0 <= 45.0 for p in paragraphs)
    rebuilt = " ".join(paragraphs)
    assert rebuilt.count("kích ứng nặng") == 14  # nothing dropped


@pytest.mark.parametrize(
    "claim, text, covered",
    [
        # negations and restrictions survive
        ("Không bảo hành khi giặt máy.", "Không bảo hành khi giặt máy nhé.", True),
        ("Không bảo hành khi giặt máy.", "Có bảo hành khi giặt máy nhé.", False),
        ("Chỉ áp dụng cho lỗi sản xuất.", "Áp dụng cho lỗi sản xuất nhé.", False),
        ("Giảm 20% vào thứ Tư đầu tháng.", "Giảm hai mươi phần trăm vào thứ tư đầu tháng.", True),
        ("Giảm 20% vào thứ Tư đầu tháng.", "Giảm hai mươi phần trăm vào thứ năm đầu tháng.", False),
        ("Giảm 20% vào thứ Tư đầu tháng.", "Giảm hai mươi phần trăm vào thứ tư nhé.", False),
        # maximal runs
        ("Giá 299 nghìn.", "Giá một triệu hai trăm chín mươi chín nghìn.", False),
        ("Cao 44.", "Cao một trăm bốn mươi bốn.", False),
        ("Ngày 1.", "Ngày thứ mười một.", False),
        ("Bảo hành 1 năm.", "Bảo hành mười một năm.", False),
        # number words in the claim itself behave like digits
        ("Đổi trả trong bảy ngày.", "Đổi trả trong bảy ngày nhé.", True),
        ("Đổi trả trong bảy ngày.", "Đổi trả trong hai mươi bảy ngày nhé.", False),
        ("Bảo hành một năm.", "Bảo hành một năm nhé.", True),
        ("Bảo hành một năm.", "Bảo hành hai mươi mốt năm nhé.", False),
        ("Hàng về năm nay.", "Hàng về năm nay nhé.", True),
        # uppercase sizes are exact
        ("Có size XL.", "Có size XXL nhé.", False),
        ("Có size XL.", "Có size XL nhé.", True),
        ("Có size M, L.", "Có size M nhé.", False),
    ],
)
def test_restrictions_timing_runs_and_sizes_are_preserved(claim, text, covered) -> None:
    from backend.application.script_authoring.generation.ordered_units import claim_covered

    assert claim_covered(claim, text) is covered


@pytest.mark.parametrize(
    "claim",
    [
        "Cao dưới 1m65, 45-60kg: size M.",
        "Cao 1m70 đến 1m75, nặng 65-72kg: size L, không đổi size.",
        "Giá 299.000đ cho mỗi hộp.",
        "Giảm 20% vào thứ Tư đầu tháng.",
        "Đổi trả trong 7 ngày kể từ khi nhận hàng.",
        "Bảo hành 12 tháng, chỉ áp dụng cho lỗi sản xuất.",
        "Dung tích 1,25 lít.",
        "Có size XL và XXL.",
        "Bảo quản ở nhiệt độ <5 hoặc >40 độ C.",
        "Gọi 0901234567 để được tư vấn.",
    ],
)
def test_every_claim_covers_itself(claim) -> None:
    from backend.application.script_authoring.generation.ordered_units import claim_covered

    assert claim_covered(claim, claim)
    assert claim_covered(claim, f"Mọi người nghe nhé. {claim} Cảm ơn mọi người.")


def test_lone_nam_before_a_count_unit_is_the_number_five() -> None:
    from backend.application.script_authoring.generation.ordered_units import claim_covered

    assert not claim_covered("Giảm năm phần trăm.", "Giảm mười phần trăm.")
    assert claim_covered("Giảm năm phần trăm.", "Giảm năm phần trăm nhé.")
    assert claim_covered("Bảo hành một năm.", "Bảo hành 1 năm nhé.")  # still the year
    assert not claim_covered("Bảo hành một năm.", "Bảo hành hai mươi mốt năm nhé.")


def test_size_chart_without_cao_is_still_one_sizes_unit() -> None:
    rows = (
        "Dưới 1m65, 45-60kg: Size M",
        "1m65-1m70, 60-70kg: Size L",
        "Hơn 1m70, 70-80kg: Size XL",
    )
    units = plan_units(
        ProductBrief(product_id="p", name="Áo", claims=rows), first=False, last=False
    )
    sizes = [u for u in units if u.role == "sizes"]
    assert len(sizes) == 1 and sizes[0].claims == rows


def test_known_typos_and_shorthand_are_fixed_and_cheap_price_claim_is_rejected() -> None:
    assert (
        check_unit_text("Cam kết đúng mẫi và đúng màu nhé.")[0]
        == "Cam kết đúng mẫu và đúng màu nhé."
    )
    valid = "Mang giày đi quan sát địa hình rất tiện."
    assert check_unit_text(valid)[0] == valid  # valid words untouched
    assert check_unit_text("Áo này giá rẻ nhé.") == (None, "hard_sell")
    brief = ProductBrief(
        product_id="p", name="Áo", claims=("Có đổi size được kh? Có, trong 15 ngày.",)
    )
    text = fallback_unit_text(UnitSpec("assurance", claims=brief.claims), brief)
    assert text.startswith("Nhiều bạn hỏi:") and "kh?" not in text and "không?" in text


def test_garment_weight_facts_are_not_a_size_chart_and_approved_cheapness_passes() -> None:
    rows = tuple(f"Khối lượng áo là {i}kg ở size {s}." for i, s in ((1, "M"), (2, "L"), (3, "XL")))
    units = plan_units(
        ProductBrief(product_id="p", name="Áo", claims=rows), first=False, last=False
    )
    assert not [u for u in units if u.role == "sizes"]
    text = "Mẫu này không phải giá rẻ, chất lượng được ưu tiên."
    assert check_unit_text(text, approved=(text,))[0]


def test_fallback_claims_are_separated_by_sentence_ends() -> None:
    brief = ProductBrief(product_id="p", name="Áo", claims=("Hai màu xanh", "Có túi sâu"))
    text = fallback_unit_text(UnitSpec("highlight", claims=brief.claims), brief)
    assert text == "Hai màu xanh. Có túi sâu."


def test_typed_highlights_fill_two_units_and_offer_may_name_the_product() -> None:
    claims = tuple(f"Đặc điểm số {i}." for i in range(12))
    brief = ProductBrief(
        product_id="p", name="Áo", claims=claims, claims_by_type=(("feature", claims),)
    )
    units = plan_units(brief, first=False, last=False)
    assert len([u for u in units if u.role == "highlight"]) == 2
    text = "Áo này giá một trăm nghìn đồng nhé."
    spec = UnitSpec("offer")
    assert check_unit_text(text, spec=spec, product_name="Áo", name_used_elsewhere=5)[0]


def test_sentence_end_is_not_doubled_after_closing_quotes_or_brackets() -> None:
    brief = ProductBrief(
        product_id="p", name="Áo", claims=("Có túi sâu.)", "Hai màu", "Bền “thật”")
    )
    text = fallback_unit_text(UnitSpec("highlight", claims=brief.claims), brief)
    assert text == "Có túi sâu.) Hai màu. Bền “thật”."


def test_prompt_asks_for_natural_longer_speech_but_keeps_facts_strict() -> None:
    brief = ProductBrief(product_id="p", name="Áo", prices=("100000.00 VND",), claims=("Vải dày.",))
    units = plan_units(brief, first=False, last=False)
    prompt = build_unit_prompt(SessionBrief(title="Live"), brief, units, 1)
    assert "câu" in prompt and "ngắn gọn" not in prompt  # no longer told to stay short
    assert "SỰ THẬT" in prompt and "KHÔNG phải kịch bản để đọc" in prompt  # facts, not a script
    assert "KHÔNG thêm: con số, giá" in prompt  # the false-information ban stays


def test_unapproved_superlatives_are_rejected_but_approved_ones_pass() -> None:
    assert check_unit_text("Đây là mẫu bán chạy nhất đó mọi người.") == (None, "hard_sell")
    said = "Mẫu này bán chạy nhất tuần qua."
    assert check_unit_text(said, approved=(said,))[0]


def test_approving_one_superlative_does_not_allow_another() -> None:
    said = "Mẫu này bán chạy nhất tuần qua."
    assert check_unit_text(said + " Hàng nghìn khách đã mua mẫu này.", approved=(said,)) == (
        None,
        "hard_sell",
    )


def test_urgency_is_hard_sell_but_following_invitation_is_fine_in_the_closing() -> None:
    assert check_unit_text("Mọi người tranh thủ lên đơn sớm nhé.") == (None, "hard_sell")
    closing = UnitSpec("closing")
    text = "Nhấn theo dõi để không bỏ lỡ những buổi live tiếp theo nhé."
    assert check_unit_text(text, spec=closing)[0]
    assert check_unit_text(text, spec=UnitSpec("highlight")) == (None, "hard_sell")
