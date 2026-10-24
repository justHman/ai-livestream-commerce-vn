"""Persisted part briefs, per-part provenance and structured save (fake LLM/repos, real gate)."""

from __future__ import annotations

import asyncio

import pytest
from test_ordered_generation import FakeLLM, _new_set, _service, _text
from test_regenerate_part import RepairLLM, wait_job

from backend.application.script_authoring.generation.ordered_units import UnitSpec
from backend.application.script_authoring.generation.unit_brief import (
    fallback_reason_code,
    find_brief,
    recap_violation,
    spec_from_brief,
)
from backend.application.script_authoring.models import ScriptState
from backend.application.script_authoring.service import ScriptAuthoringError


async def _generate(service, repos, set_id, seconds=300):
    started = await service.start_batch_generation(
        set_id=set_id,
        product_ids=[],
        target_duration_s=seconds,
        idempotency_key="k",
        ordered_units=True,
    )
    for _ in range(600):
        _batch, state = repos.batches.rows[started["batch_id"]]
        if state.status in ("completed", "partial_completed", "failed", "cancelled"):
            return
        await asyncio.sleep(0.02)
    pytest.fail("batch did not finish")


async def _parts(service, set_id, pid="P1"):
    wire = await service.get_script_set(set_id=set_id)
    return wire["items"][pid]["current_version"]


def test_fallback_reasons_are_normalized_from_raw_codes():
    cases = {
        "guard:coverage": "coverage_missing",
        "missing_info": "coverage_missing",
        "CLAIM_FACTUAL": "facts_mismatch",
        "guard:truncated": "unfinished_text",
        "guard:hard_sell": "wording_unsuitable",
        "SPEECH_DURATION_SEGMENT": "wording_unsuitable",
        "llm_failed:LLMClientError": "provider_unavailable",
        "llm_failed:LLMDeadlineError": "time_budget_exhausted",
        "semantic_budget_exhausted": "time_budget_exhausted",
        "???": "unknown",
    }
    for raw, code in cases.items():
        assert fallback_reason_code([raw]) == code
    assert fallback_reason_code(["CLAIM_FACTUAL", "guard:coverage"]) == "coverage_missing"
    assert fallback_reason_code([]) == "unknown"


def test_recap_must_not_open_as_a_first_time_introduction():
    assert recap_violation("Mình giới thiệu Gel XYZ đến mọi người.")
    assert recap_violation("Chào mọi người, đây là Gel XYZ.")
    assert not recap_violation("Mình nhắc lại nhanh cho bạn mới vào: gel làm sạch nhẹ nhàng.")


@pytest.mark.asyncio
async def test_brief_is_saved_before_writing_and_each_part_has_provenance():
    llm = FakeLLM(empty_for={("Gel XYZ", "Điểm nổi bật")})  # never writes the highlight
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await _generate(service, repos, set_id)
    version = await _parts(service, set_id)
    parts = version["parts"]
    assert all(p["part_id"] and p["segment_id"] for p in parts)
    assert {p["source"] for p in parts} == {"llm", "fallback"}
    fallback = [p for p in parts if p["source"] == "fallback"]
    assert fallback and all(
        p["needs_regeneration"] and p["fallback_reason_code"] == "wording_unsuitable"
        for p in fallback
    )
    ok = next(p for p in parts if p["source"] == "llm")
    assert ok["fallback_reason_code"] is None and "needs_regeneration" not in ok
    assert set(ok["brief"]) == {"schema_version", "role", "speech_act", "purpose"}
    assert ok["repeated_opening"] is False
    assert version["needs_attention_count"] == len(fallback)
    item, _ = await _text(repos, set_id, "P1")
    segment = await repos.segments.get(parts[0]["segment_id"])
    plan = await repos.plans.get(segment.plan_id)
    assert find_brief(plan.unit_briefs, segment.part_id)["role"] == parts[0]["brief"]["role"]
    assert [b["part_id"] for b in plan.unit_briefs["parts"]] == [p["part_id"] for p in parts]


@pytest.mark.asyncio
async def test_legacy_segment_without_provenance_is_never_reported_as_llm():
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _generate(service, repos, set_id)
    version = await _parts(service, set_id)
    first, second = version["parts"][0], version["parts"][1]
    for p, status in ((first, ScriptState.DRAFT), (second, ScriptState.GATE_FAILED)):
        row = repos.segments.rows[p["segment_id"]]
        repos.segments.rows[p["segment_id"]] = row.model_copy(
            update={"provenance": None, "part_id": None, "status": status}
        )
    legacy = (await _parts(service, set_id))["parts"]
    assert legacy[0]["source"] is None and legacy[0]["fallback_reason_code"] is None
    assert "needs_regeneration" not in legacy[0] and legacy[0]["brief"] is None
    assert legacy[1]["source"] is None and legacy[1]["needs_regeneration"] is True
    assert legacy[1]["fallback_reason_code"] == "unknown"


@pytest.mark.asyncio
async def test_regenerating_a_recap_part_keeps_the_stored_recap_intent():
    llm = RepairLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await _generate(service, repos, set_id)
    _, before = await _text(repos, set_id, "P2")
    version = await _parts(service, set_id, "P2")
    recap = next(p for p in version["parts"] if p["brief"]["speech_act"] == "recap")
    assert recap["brief"]["role"] in ("highlight", "assurance", "sizes", "offer")
    stored = await repos.plans.get((await repos.segments.get(recap["segment_id"])).plan_id)
    # Only the stored brief can explain this duration: a re-plan would compute another one.
    target = next(b for b in stored.unit_briefs["parts"] if b["part_id"] == recap["part_id"])
    target["target_seconds"] = 10.0
    llm.prompts.clear()
    seen = []

    def reply(prompt):
        seen.append(prompt)
        if len(seen) == 1:
            return "Mình giới thiệu Gel XYZ đến mọi người, đây là mẫu rất dễ dùng."
        return "Mình nhắc lại nhanh cho bạn mới vào nhé. " + " ".join(
            c for c in P2_ITEMS if c in prompt
        )

    llm.repair = reply
    started = await service.regenerate_segment(
        set_id=set_id,
        product_id="P2",
        segment_index=999,  # index is only a compatibility key; part_id wins
        part_id=recap["part_id"],
        idempotency_key="recap",
        expected_version_id=before.id,
    )
    status = await wait_job(service, set_id, "P2", started["workflow_id"])
    assert status["status"] == "completed", status
    assert len(seen) == 2  # intro-style candidate rejected by the recap guard
    assert "136 đến 184 ký tự" in seen[0]
    assert "NHẮC LẠI" in seen[0] and "Phần liền trước trong kịch bản" in seen[0]
    after = (await _parts(service, set_id, "P2"))["parts"]
    new = next(p for p in after if p["part_id"] == recap["part_id"])
    assert new["segment_id"] != recap["segment_id"] and new["source"] == "llm"
    assert new["brief"]["speech_act"] == "recap" and new["display_text"].startswith("Mình nhắc lại")
    others = [p["segment_id"] for p in after if p["part_id"] != recap["part_id"]]
    assert others == [p["segment_id"] for p in version["parts"] if p["part_id"] != recap["part_id"]]


P2_ITEMS = [
    "Kem dưỡng ẩm sâu cho tóc khô.",
    "Chất kem mềm mượt dễ thoa.",
    "Bảo hành 1 năm.",
    "Giảm 20% vào thứ Tư đầu tháng.",
]


@pytest.mark.asyncio
async def test_regenerating_a_fallback_part_clears_its_attention_flag():
    llm = RepairLLM(empty_for={("Gel XYZ", "Điểm nổi bật")})
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await _generate(service, repos, set_id, seconds=60)
    _, before = await _text(repos, set_id, "P1")
    parts = (await _parts(service, set_id))["parts"]
    bad = next(p for p in parts if p["source"] == "fallback")
    llm.empty_for = set()
    started = await service.regenerate_segment(
        set_id=set_id,
        product_id="P1",
        segment_index=bad["segment_index"],
        part_id=bad["part_id"],
        idempotency_key="fix",
        expected_version_id=before.id,
    )
    assert (await wait_job(service, set_id, "P1", started["workflow_id"]))["status"] == "completed"
    version = await _parts(service, set_id)
    fixed = next(p for p in version["parts"] if p["part_id"] == bad["part_id"])
    assert fixed["source"] == "llm" and "needs_regeneration" not in fixed
    assert version["needs_attention_count"] == 0


@pytest.mark.asyncio
async def test_structured_save_keeps_identity_and_marks_only_edits_manual():
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _generate(service, repos, set_id, seconds=60)
    item, before = await _text(repos, set_id, "P1")
    old = (await _parts(service, set_id))["parts"]
    payload = [
        {"part_id": p["part_id"], "segment_id": p["segment_id"], "display_text": p["display_text"]}
        for p in old
    ]
    payload[1]["display_text"] = "Mình sửa tay phần này.\n\nVẫn là cùng một phần."
    payload.append({"part_id": None, "segment_id": None, "display_text": "Phần mới thêm."})
    result = await service.save_draft_parts(
        set_id=set_id, product_id="P1", parts=payload, expected_version_id=before.id
    )
    assert result["ok"]
    _, after = await _text(repos, set_id, "P1")
    assert after.id != before.id and after.source.value == "manual"
    new = (await _parts(service, set_id))["parts"]
    assert len(new) == len(old) + 1
    assert [p["segment_id"] for i, p in enumerate(new) if i not in (1, len(old))] == [
        p["segment_id"] for i, p in enumerate(old) if i != 1
    ]
    assert [p["source"] for p in new[:1]] == [old[0]["source"]]
    assert new[1]["part_id"] == old[1]["part_id"] and new[1]["segment_id"] != old[1]["segment_id"]
    assert new[1]["source"] == "manual" and new[1]["brief"] == old[1]["brief"]
    assert "\n\n" in new[1]["display_text"]  # one part, several paragraphs, intact
    assert new[-1]["source"] == "manual" and new[-1]["part_id"] not in {p["part_id"] for p in old}
    assert new[-1]["brief"] is None
    # same payload again is a no-op; an old version id is stale
    again = [
        {"part_id": p["part_id"], "segment_id": p["segment_id"], "display_text": p["display_text"]}
        for p in new
    ]
    await service.save_draft_parts(
        set_id=set_id, product_id="P1", parts=again, expected_version_id=after.id
    )
    assert (await _text(repos, set_id, "P1"))[1].id == after.id
    with pytest.raises(ScriptAuthoringError) as stale:
        await service.save_draft_parts(
            set_id=set_id, product_id="P1", parts=again, expected_version_id=before.id
        )
    assert stale.value.code == "stale_revision"


@pytest.mark.asyncio
async def test_text_only_save_still_works_without_part_provenance():
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _generate(service, repos, set_id, seconds=60)
    _, before = await _text(repos, set_id, "P1")
    await service.save_draft(
        set_id=set_id,
        product_id="P1",
        display_text=before.display_text + "\nThêm một câu.",
        spoken_text=None,
        revision=None,
    )
    version = await _parts(service, set_id)
    assert "parts" not in version and version["needs_attention_count"] == 0
    assert version["source"] == "manual"


def test_brief_round_trips_to_the_same_unit_spec():
    from backend.application.script_authoring.generation.ordered_units import ProductBrief
    from backend.application.script_authoring.generation.unit_brief import build_unit_briefs

    spec = UnitSpec(
        "highlight",
        claims=("A.", "B."),
        promos=("P.",),
        must=("B.",),
        with_cta=True,
        revisit=True,
        target_seconds=28.0,
    )
    doc = build_unit_briefs([spec], ProductBrief(product_id="X", name="X"), lambda: "part:1")
    assert doc["parts"][0]["speech_act"] == "recap"
    assert spec_from_brief(doc["parts"][0]) == spec
