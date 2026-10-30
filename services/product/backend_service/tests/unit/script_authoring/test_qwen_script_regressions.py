"""2026-10-24 production-log regressions; no hosted LLM calls."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    BoundedLLM,
    SessionBrief,
    UnitSpec,
    build_unit_prompt,
    build_unit_repair_prompt,
    check_unit_text,
    claim_covered,
    fallback_unit_text,
    retry_hint,
)
from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.rules.commerce_claims import check_factual_claims
from test_regenerate_part import shipping_part, repair_part
from test_ordered_generation import FakeLLM, _service, _new_set, _run
from backend.application.script_authoring.repositories import JobRepository, LeaseLostError
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl


SHIPPING = "Giao hàng toàn quốc từ 2-3 ngày, nội thành HCM và HN ship hỏa tốc trong 1 ngày"
PARAPHRASE = (
    "Giao hàng toàn quốc trong hai đến ba ngày, riêng nội thành Hà Nội và TP.HCM "
    "ship hỏa tốc trong một ngày."
)


def test_log_intro_prefix_is_not_itself_an_unsupported_claim():
    assert not check_factual_claims(
        "Chào mọi người, mình xin giới thiệu ngay mẫu áo khoác denim thời trang từ Icon Denim.",
        ScriptGateContext(facts=ProductFacts()),
    )


def test_unapproved_durability_remains_blocked():
    assert any(
        v.severity.value == "error"
        for v in check_factual_claims(
            "Chất liệu denim dày dặn, chắc chắn bền bỉ.",
            ScriptGateContext(facts=ProductFacts(allowed_claims=("Chất liệu denim.",))),
        )
    )


def test_shipping_words_and_city_aliases_cover_the_log_claim():
    assert claim_covered(SHIPPING, PARAPHRASE)


def test_complete_shipping_information_fits_the_existing_duration_and_safety_gate():
    result = ScriptAuthoringServiceImpl._default_gate().run_segment(
        PARAPHRASE,
        ScriptGateContext(
            facts=ProductFacts(allowed_claims=(SHIPPING,)),
            target_min_seconds=1,
            target_max_seconds=60,
        ),
    )
    assert result.passed, result.violations


def test_ordered_duration_repair_preserves_values_without_requesting_numeric_prices():
    result = ScriptAuthoringServiceImpl._default_gate().run_segment(
        "Giá một trăm tám mươi nghìn đồng. " * 30,
        ScriptGateContext(target_min_seconds=1, target_max_seconds=60),
    )
    violation = next(v for v in result.violations if v.rule_id == "SPEECH_DURATION_SEGMENT")
    prompt = build_unit_repair_prompt(
        SessionBrief(),
        ProductBrief("P", "Áo khoác", prices=("180000 VND",)),
        [UnitSpec("offer", with_price=True)],
        0,
        failed_text="Bản quá dài.",
        problems=[f"{violation.rule_id}: {violation.message}"],
    )
    assert "KEEP the compact" not in prompt
    assert "SPEECH_DURATION_SEGMENT: Segment is too long:" in prompt
    assert "giá vẫn viết bằng chữ" in prompt


def test_scope_split_preserves_a_leading_restriction():
    assert not claim_covered("Chỉ " + SHIPPING, PARAPHRASE)


def test_revisit_fallback_has_a_different_lead_and_keeps_all_approved_conditions():
    product = ProductBrief("P", "Áo khoác", claims=(SHIPPING,))
    first = fallback_unit_text(UnitSpec("assurance", claims=(SHIPPING,)), product)
    revisit = fallback_unit_text(UnitSpec("assurance", claims=(SHIPPING,), revisit=True), product)
    assert first != revisit
    assert first in revisit


@pytest.mark.parametrize(
    "text",
    [
        PARAPHRASE.replace("hai đến ba", "hai đến bốn"),
        PARAPHRASE.replace("một ngày", "hai ngày"),
        PARAPHRASE.replace("nội thành", "ngoại thành"),
        PARAPHRASE.replace("Hà Nội và TP.HCM", "Đà Nẵng"),
        "Giao hàng toàn quốc trong một ngày, nội thành Hà Nội và TP.HCM trong hai đến ba ngày.",
    ],
)
def test_shipping_normalization_never_loses_terms_or_swaps_numbers(text):
    assert not claim_covered(SHIPPING, text)


def test_cjk_is_a_targeted_retry_before_other_wording_checks():
    text = "Áo khoác có ba màu, ai cũng能找到 màu hợp gu cho bạn nam."
    assert check_unit_text(text) == (None, "foreign_script")
    hint = retry_hint("foreign_script", text)
    assert "tiếng Việt" in hint and "能找到" in hint


def test_prompt_does_not_request_personal_experience_or_unsourced_benefits():
    prompt = build_unit_prompt(
        SessionBrief(), ProductBrief("P", "Áo khoác"), [UnitSpec("highlight")], 0
    )
    assert "lợi ích suy ra hợp lý" not in prompt
    assert "không bịa trải nghiệm cá nhân" in prompt
    assert "Chỉ dùng tiếng Việt" in prompt


@pytest.mark.asyncio
async def test_part_foreign_script_retry_receives_specific_hint_and_keeps_facts():
    service, repos, llm, set_id, before, index = await shipping_part()

    def reply(prompt):
        if len(llm.prompts) == 1:
            return "Giao hàng trong ba ngày. Không giao hàng vào chủ nhật. 大家好."
        assert "大家好" in prompt and "tiếng Việt" in prompt
        return "Giao hàng trong ba ngày. Không giao hàng vào chủ nhật."

    llm.repair = reply
    status = await repair_part(service, set_id, before, index)
    assert status["status"] == "completed"
    assert len(llm.prompts) == 2


@pytest.mark.asyncio
async def test_full_generation_shares_three_calls_across_foreign_and_truncated_repairs():
    class MixedLLM(FakeLLM):
        attempts = 0

        def __call__(self, prompt):
            if "Tên sản phẩm: Kem ABC" in prompt and "): Điểm nổi bật." in prompt:
                self.attempts += 1
                if self.attempts < 3:
                    self.prompts.append(prompt)
                    return "Mẫu này đẹp能找到." if self.attempts == 1 else "Mẫu này chưa kết thúc"
                # Each retry repairs the latest rejected text for its own reason.
                assert "Mẫu này chưa kết thúc" in prompt and "NGẮN HƠN" in prompt
            return super().__call__(prompt)

    llm = MixedLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch = await _run(service, repos, set_id)
    assert llm.attempts == 3
    status = await service.get_batch(set_id=set_id, batch_id=batch)
    assert all("fallback_units" not in product for product in status["products"])


@pytest.mark.asyncio
async def test_fallback_part_is_marked_in_the_persisted_review_read_model():
    llm = FakeLLM(empty_for={("Gel XYZ", "Điểm nổi bật")})
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    repos.items.list_by_set = AsyncMock(return_value=list(repos.items.rows.values()))
    repos.gate_runs.latest_for_version = AsyncMock(return_value=None)
    read = await service.get_script_set(set_id=set_id)
    parts = read["items"]["P1"]["current_version"]["parts"]
    assert [p["segment_index"] for p in parts if p.get("needs_regeneration")] == [1]


def test_larger_token_budget_is_only_used_for_repairs():
    calls = []
    llm = BoundedLLM(
        lambda p: calls.append((900, p)) or "OK",
        repair_fn=lambda p: calls.append((1400, p)) or "Đã sửa.",
        attempts=1,
    )
    assert llm("first") == "OK"
    assert llm("second", repair=True) == "Đã sửa."
    assert calls == [(900, "first"), (1400, "second")]


@pytest.mark.parametrize(
    "text",
    [
        "Mình đã mặc thử mẫu này, thấy rất thoải mái.",
        "Mình rất ấn tượng với chất liệu denim dày dặn, sờ vào thấy chắc chắn và bền bỉ.",
    ],
)
def test_fabricated_personal_experience_is_retried(text):
    assert check_unit_text(text) == (None, "personal_experience")


@pytest.mark.asyncio
@pytest.mark.parametrize("fenced", [False, True])
async def test_last_rejection_is_durable_and_keeps_job_fencing(fenced):
    # Exercise the production repository serialization, not just mutable fake rows.
    import json

    service, repos, llm, set_id, before, index = await shipping_part()
    llm.repair = lambda p: "Giao hàng trong ba ngày."
    status = await repair_part(service, set_id, before, index)
    job = await repos.jobs.get(status["workflow_id"])
    repository = JobRepository(SimpleNamespace())
    execute = AsyncMock(return_value="UPDATE 1")
    repository._execute = execute
    kwargs = {"lease_owner": "worker", "lease_epoch": 2} if fenced else {}
    await repository.update(job, **kwargs)
    sql, *args = execute.call_args.args
    assert json.loads(args[-1])["last_rejection"] == status["error_message"]
    assert "input_snapshot=" in sql
    if fenced:
        assert "AND lease_owner=$8 AND lease_epoch=$9" in sql
        execute.return_value = "UPDATE 0"
        with pytest.raises(LeaseLostError):
            await repository.update(job, **kwargs)
