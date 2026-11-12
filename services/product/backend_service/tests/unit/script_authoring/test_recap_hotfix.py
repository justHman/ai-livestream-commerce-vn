"""Recap, offer and non-opening guard regressions; fake model only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    SessionBrief,
    UnitSpec,
    build_unit_prompt,
    check_unit_text,
    fallback_unit_text,
    salvage_unit_text,
)
from backend.application.script_authoring.generation.script_variety import VarietyPart, new_repeats
from backend.application.script_authoring.service_impl import _compile_without_duplicate_recaps


def test_recap_prefix_exemption_does_not_exempt_normal_parts_or_recap_bodies():
    parts = [
        VarietyPart("a", "p", "highlight", "Mình quay lại phần vải. Vải cotton mềm.", revisit=True),
        VarietyPart("b", "p", "highlight", "Một cách dẫn khác. Có hai màu.", revisit=True),
    ]
    assert not new_repeats(parts, "b", "Mình quay lại phần màu. Có hai màu.")
    normal = [replace(p, revisit=False) for p in parts]
    assert new_repeats(normal, "b", "Mình quay lại phần màu. Có hai màu.")
    assert new_repeats(parts, "b", "Lời dẫn riêng. Vải cotton mềm và dày.")


def test_recap_leads_rotate_and_prompts_describe_different_rules():
    product = ProductBrief(product_id="p", name="Áo XYZ")
    specs = [
        UnitSpec("highlight", claims=("Vải cotton.",), revisit=True, recap_index=i)
        for i in range(2)
    ]
    texts = [fallback_unit_text(s, product) for s in specs]
    assert texts[0].split()[:3] != texts[1].split()[:3]
    prompts = [build_unit_prompt(SessionBrief(), product, specs, i) for i in range(2)]
    assert "nhắc lại ý chính cho người mới theo dõi" in prompts[0]
    assert "chuyển từ ý trước sang lời tóm lược" in prompts[1]
    assert all(text not in prompt for text in texts for prompt in prompts)


@pytest.mark.parametrize("source,keep_recap", [("fallback", False), ("llm", True)])
def test_duplicate_extractive_recap_is_dropped_before_compile(source, keep_recap):
    main = SimpleNamespace(
        display_text="Vải cotton. Có hai màu.", provenance={"source": "fallback"}
    )
    recap = SimpleNamespace(
        display_text="Mình nhắc lại nhé. Vải cotton. Có hai màu.",
        provenance={"source": source},
    )
    reminder = SimpleNamespace(
        display_text="Giá một trăm nghìn đồng.", provenance={"source": "fallback"}
    )
    rows = {0: main, 1: recap, 2: reminder}
    workflow = SimpleNamespace(segments=rows, plan_segment_count=3)
    roles = [
        UnitSpec("highlight"),
        UnitSpec("highlight", revisit=True),
        UnitSpec("offer", revisit=True),
    ]

    def compile_selected():
        selected = list(workflow.segments.values())
        assert (recap in selected) is keep_recap
        assert main in selected and reminder in selected
        assert workflow.plan_segment_count == len(selected)
        return "compiled"

    assert _compile_without_duplicate_recaps(workflow, roles, compile_selected) == "compiled"
    assert workflow.segments is rows and workflow.plan_segment_count == 3


def test_offer_dedupes_equivalent_price_not_different_conditions():
    text, reason = check_unit_text(
        "Mua 2 cái giá 650.000đ. Mua 2 cái giá 650.000 đồng.",
        spec=UnitSpec("offer", promos=("Mua 2 cái giá 650.000đ.",)),
        prices=("650000 VND",),
        approved=("Mua 2 cái giá 650.000đ.",),
    )
    assert reason is None
    assert text.count("sáu trăm năm mươi nghìn đồng") == 1
    different = "Mua 2 cái giá 650.000đ. Mua 3 cái giá 650.000 đồng."
    text, reason = check_unit_text(
        different,
        spec=UnitSpec("offer", promos=(different,)),
        prices=("650000 VND",),
        approved=(different,),
    )
    assert reason is None
    assert "Mua 2 cái" in text and "Mua 3 cái" in text


@pytest.mark.parametrize("role", ["intro", "highlight", "sizes", "assurance", "offer", "closing"])
@pytest.mark.parametrize("lead", ["Chào buổi chiều mọi người, ", "Xin chào mọi người! "])
def test_greeting_is_cut_from_every_non_opening_role(role, lead):
    spec = UnitSpec(role)
    body = "Mình giới thiệu Áo XYZ đến mọi người."
    kwargs = {"spec": spec, "product_name": "Áo XYZ"}
    assert check_unit_text(lead + body, **kwargs)[1] == "greeting"
    assert salvage_unit_text(lead + body, "greeting", **kwargs) == body
    assert check_unit_text(lead + body, spec=UnitSpec("opening"))[1] is None


@pytest.mark.parametrize(
    "phrase",
    ["hot item", "rất đã", "cho cả lố", "cho cả lô", "nâng tầm trải nghiệm", "lựa chọn lý tưởng"],
)
def test_hype_requires_owner_wording(phrase):
    text = f"Đây là {phrase}."
    assert check_unit_text(text, spec=UnitSpec("highlight"))[1] == "hard_sell"
    assert check_unit_text(text, spec=UnitSpec("highlight"), approved=(text,))[1] is None


def test_warranty_does_not_gain_normal_qualification():
    text = "Bảo hành 1 năm bình thường."
    assert (
        check_unit_text(text, spec=UnitSpec("assurance"), approved=("Bảo hành 1 năm.",))[1]
        == "hard_sell"
    )
    assert check_unit_text(text, spec=UnitSpec("assurance"), approved=(text,))[1] is None
    assert (
        salvage_unit_text(
            text, "hard_sell", spec=UnitSpec("assurance"), approved=("Bảo hành 1 năm.",)
        )
        == "Bảo hành 1 năm."
    )


@pytest.mark.asyncio
async def test_regenerate_recap_accepts_a_shared_neutral_lead_in(monkeypatch):
    from test_ordered_generation import _service, _text
    from test_regenerate_part import RepairLLM, wait_job
    from test_script_brief import _generate, _new_rich_set, _parts
    from backend.application.script_authoring.generation import variety_finalizer
    from backend.application.script_authoring import service_impl

    monkeypatch.setattr(service_impl, "_compile_without_duplicate_recaps", lambda w, r, c: c())

    llm = RepairLLM()
    service, repos = _service(llm)
    set_id = await _new_rich_set(service)
    await _generate(service, repos, set_id)
    _, before = await _text(repos, set_id, "P2")
    parts = (await _parts(service, set_id, "P2"))["parts"]
    recap = next(p for p in parts if p["brief"]["speech_act"] == "recap")
    segment = await repos.segments.get(recap["segment_id"])
    plan = await repos.plans.get(segment.plan_id)
    brief = next(b for b in plan.unit_briefs["parts"] if b["part_id"] == recap["part_id"])
    body = brief["required_facts"][0]["text"].translate(str.maketrans("1234", "abcd"))
    candidate = "Mình quay lại phần thông tin này. " + body
    real_selected = variety_finalizer.selected_parts

    async def selected(*args):
        selected_parts, records = await real_selected(*args)
        # A sibling recap has the same neutral lead-in, but different facts.
        selected_parts.append(
            VarietyPart(
                "sibling:recap",
                "P2",
                brief["role"],
                "Mình quay lại phần màu sắc. Có màu xanh.",
                revisit=True,
            )
        )
        return selected_parts, records

    monkeypatch.setattr(variety_finalizer, "selected_parts", selected)
    llm.prompts.clear()
    llm.repair = lambda prompt: candidate
    started = await service.regenerate_segment(
        set_id=set_id,
        product_id="P2",
        segment_index=segment.segment_index,
        part_id=recap["part_id"],
        idempotency_key="shared-recap-lead",
        expected_version_id=before.id,
    )
    status = await wait_job(service, set_id, "P2", started["workflow_id"])
    assert status["status"] == "completed", status
    assert len(llm.prompts) == 1


@pytest.mark.asyncio
async def test_regenerate_intro_checks_the_product_name():
    from test_regenerate_part import shipping_part, repair_part

    service, repos, llm, set_id, before, _index = await shipping_part()
    segments = [await repos.segments.get(sid) for sid in before.segment_version_ids]
    plan = await repos.plans.get(segments[0].plan_id)
    intro_id = next(b["part_id"] for b in plan.unit_briefs["parts"] if b["role"] == "intro")
    intro = next(s for s in segments if s.part_id == intro_id)

    def reply(prompt):
        if len(llm.prompts) == 1:
            return "Mình giới thiệu sản phẩm này đến mọi người."
        return "Mình giới thiệu Gel XYZ đến mọi người."

    llm.repair = reply
    status = await repair_part(service, set_id, before, intro.segment_index)
    assert status["status"] == "completed", status
    assert len(llm.prompts) == 2
