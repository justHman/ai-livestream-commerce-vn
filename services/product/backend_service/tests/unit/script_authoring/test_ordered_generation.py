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
    clean_unit_text,
    plan_units,
    spoken_price,
)
from backend.application.script_authoring.models import ScriptState
from backend.application.script_authoring.service import ScriptAuthoringError
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
async def test_failed_unit_keeps_the_other_units_and_blocks_approval() -> None:
    llm = FakeLLM(empty_for={("Gel XYZ", "Giá và ưu đãi")})
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)

    item, version = await _text(repos, set_id, "P1")
    units = split_units(version.spoken_text)
    assert item.state is ScriptState.GATE_FAILED and len(units) == 4
    assert units[2].startswith("<Phần này chưa soạn được")  # visible, gate-failing placeholder
    assert units[1] == " ".join(P1_CLAIMS)
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    statuses = {p["product_id"]: p for p in snapshot["products"]}
    assert snapshot["outcome"] == "partial"
    assert statuses["P2"]["status"] == "done" and statuses["P1"]["status"] == "failed"
    assert any(i["unit_index"] == 2 for i in statuses["P1"]["issues"])
    with pytest.raises(ScriptAuthoringError):
        await service.approve_product(
            set_id=set_id,
            product_id="P1",
            version_id=version.id,
            actor="owner",
            is_human=True,
            authorized=True,
        )


@pytest.mark.asyncio
async def test_provider_errors_are_bounded_and_never_crash_the_batch(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    llm = FakeLLM(raises=LLMClientError("boom"))
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    assert snapshot["outcome"] == "failed"
    item, version = await _text(repos, set_id, "P2")
    assert item.state is ScriptState.GATE_FAILED
    assert all(u.startswith("<Phần này chưa soạn được") for u in split_units(version.spoken_text))
    assert len(llm.prompts) <= 9 * 2  # <= 2 transport attempts per unit


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
    assert clean_unit_text('"Giá tốt — mời bạn xem nhé"') == "Giá tốt, mời bạn xem nhé"
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
    service, repos = _service(FakeLLM(empty_for={("Gel XYZ", "Giá và ưu đãi")}))
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    assert (await _text(repos, set_id, "P1"))[0].state is ScriptState.GATE_FAILED
    # the failed offer unit is rewritten with an authorised price spelt "150k"
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
