"""A part repair must not replace siblings or publish rejected content."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_ordered_generation import _service, _new_set, _run, FakeLLM, _text
from backend.application.script_authoring.gate.results import (
    GateRunResult,
    RuleSetFingerprint,
    RuleViolation,
    Severity,
)
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.service_impl import _COVERAGE_RETRY, _SHORTER_RETRY


SHIPPING = ("Giao hàng trong ba ngày.", "Không giao hàng vào chủ nhật.")


class RepairLLM(FakeLLM):
    repair = None

    def __call__(self, prompt):
        if self.repair is None:
            return super().__call__(prompt)
        self.prompts.append(prompt)
        return self.repair(prompt)


async def shipping_part():
    llm = RepairLLM()
    service, repos = _service(llm)
    set_id = (
        await service.create_script_set(
            name="Giao hàng",
            transition_policy="ORDER_AWARE",
            product_ids=["P1"],
            brief={
                "product_facts": {
                    "P1": {
                        "product_name": "Gel XYZ",
                        "prices": ["100000 VND"],
                        "allowed_claims": list(SHIPPING),
                        "claims_by_type": {"shipping": list(SHIPPING)},
                    }
                }
            },
        )
    )["id"]
    await _run(service, repos, set_id)
    _, before = await _text(repos, set_id, "P1")
    segments = [await repos.segments.get(sid) for sid in before.segment_version_ids]
    index = next(s.segment_index for s in segments if all(c in s.spoken_text for c in SHIPPING))
    llm.prompts.clear()
    return service, repos, llm, set_id, before, index


async def repair_part(service, set_id, before, index):
    started = await service.regenerate_segment(
        set_id=set_id,
        product_id="P1",
        segment_index=index,
        idempotency_key="repair",
        expected_version_id=before.id,
    )
    return await wait_job(service, set_id, "P1", started["workflow_id"])


@pytest.mark.asyncio
async def test_coverage_retry_names_only_the_missing_claim():
    service, repos, llm, set_id, before, index = await shipping_part()

    def reply(prompt):
        if _COVERAGE_RETRY in prompt:
            assert prompt.split(_COVERAGE_RETRY)[-1] == SHIPPING[1]
            return " ".join(SHIPPING)
        return SHIPPING[0]

    llm.repair = reply
    status = await repair_part(service, set_id, before, index)
    assert status["status"] == "completed"
    assert len(llm.prompts) == 2
    assert "không sao chép nguyên văn" in llm.prompts[0]
    _, after = await _text(repos, set_id, "P1")
    assert after.id != before.id


@pytest.mark.asyncio
async def test_repeated_missing_claim_keeps_all_saved_content_and_reports_failure():
    service, repos, llm, set_id, before, index = await shipping_part()
    item, _ = await _text(repos, set_id, "P1")
    versions = dict(repos.versions.rows)
    segments = dict(repos.segments.rows)
    llm.repair = lambda prompt: SHIPPING[0]
    status = await repair_part(service, set_id, before, index)
    assert status["status"] == "failed"
    assert status["error_code"] == "part_content_rejected"
    assert status["error_message"] == f"Phần này chưa đạt vì thiếu: {SHIPPING[1]} Giữ bản hiện tại."
    assert len(llm.prompts) == 3
    after_item, after = await _text(repos, set_id, "P1")
    assert after_item == item and after == before
    assert repos.versions.rows == versions and repos.segments.rows == segments


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cta", "truncated", "too_long"])
async def test_guard_retries_repair_the_specific_problem(reason):
    service, repos, llm, set_id, before, index = await shipping_part()
    # CTA shares a sentence with a mandatory claim, so cutting it cannot salvage coverage.
    bad = {
        "cta": "Giao hàng trong ba ngày, nhắn tin cho mình nhé. " + SHIPPING[1],
        "truncated": " ".join(SHIPPING).rstrip("."),
        "too_long": " ".join(SHIPPING) + " Mọi người tham khảo nhé." * 50,
    }[reason]

    def reply(prompt):
        # Require both repairs before succeeding: exercises the new third attempt.
        if len(llm.prompts) < 3:
            return bad
        if reason == "cta":
            assert prompt.count("('nhắn tin')") == 2
        else:
            assert prompt.count(_SHORTER_RETRY) == 2
        return " ".join(SHIPPING)

    llm.repair = reply
    status = await repair_part(service, set_id, before, index)
    assert status["status"] == "completed" and len(llm.prompts) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1, 2, 3])
async def test_each_attempt_reserves_budget_and_never_exceeds_it(limit):
    service, repos, llm, set_id, before, index = await shipping_part()
    reserve = AsyncMock(side_effect=[True] * limit + [False])
    repos.model_budget = SimpleNamespace(reserve=reserve)
    llm.repair = lambda prompt: SHIPPING[0]
    status = await repair_part(service, set_id, before, index)
    assert len(llm.prompts) == limit
    assert reserve.await_count == min(limit + 1, 3)
    if limit > 0:
        assert (
            status["error_message"]
            == f"Phần này chưa đạt vì thiếu: {SHIPPING[1]} Giữ bản hiện tại."
        )
    assert all(c.kwargs == {"operation_id": status["workflow_id"]} for c in reserve.call_args_list)
    assert status["error_code"] == (
        "part_model_budget_exhausted" if limit == 0 else "part_content_rejected"
    )
    assert (await _text(repos, set_id, "P1"))[1] == before


@pytest.mark.asyncio
async def test_gate_retry_identifies_each_failed_rule(monkeypatch):
    service, repos, llm, set_id, before, index = await shipping_part()
    violations = (
        RuleViolation(rule_id="VN_SPELLING_TONE", severity=Severity.ERROR, message="Sửa dấu câu."),
        RuleViolation(rule_id="TTS_ACRONYM", severity=Severity.ERROR, message="Đọc thành chữ."),
        RuleViolation(
            rule_id="SPEECH_DURATION_SEGMENT", severity=Severity.ERROR, message="Rút ngắn phần."
        ),
    )
    real_gate = service._gate.run_segment
    calls = []

    def gate(text, context):
        calls.append(text)
        if len(calls) == 1:
            return GateRunResult(
                scope="segment", fingerprint=RuleSetFingerprint(), violations=violations
            )
        return real_gate(text, context)

    monkeypatch.setattr(service._gate, "run_segment", gate)

    def reply(prompt):
        if len(llm.prompts) > 1:
            for violation in violations:
                assert f"{violation.rule_id}: {violation.message}" in prompt
            assert "không sao chép nguyên văn" in prompt
        return " ".join(SHIPPING)

    llm.repair = reply
    status = await repair_part(service, set_id, before, index)
    assert status["status"] == "completed" and len(llm.prompts) == 2


@pytest.mark.asyncio
async def test_cta_sentence_is_salvaged_without_another_model_call():
    service, repos, llm, set_id, before, index = await shipping_part()
    llm.repair = lambda prompt: " ".join(SHIPPING) + " Nhắn tin cho mình nhé."
    status = await repair_part(service, set_id, before, index)
    assert status["status"] == "completed" and len(llm.prompts) == 1
    _, after = await _text(repos, set_id, "P1")
    assert "Nhắn tin" not in after.spoken_text


@pytest.mark.asyncio
async def test_rejection_does_not_overwrite_edits_saved_while_model_runs(monkeypatch):
    service, repos, llm, set_id, before, index = await shipping_part()
    entered, release = asyncio.Event(), asyncio.Event()
    heartbeat = service._with_lease_heartbeat

    async def paused_call(fn, **kwargs):
        entered.set()
        await release.wait()
        return await heartbeat(fn, **kwargs)

    monkeypatch.setattr(service, "_with_lease_heartbeat", paused_call)
    llm.repair = lambda prompt: SHIPPING[0]
    task = asyncio.create_task(repair_part(service, set_id, before, index))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await service.save_draft(
            set_id=set_id,
            product_id="P1",
            display_text=before.display_text + "\nBản mình đã sửa.",
            spoken_text=None,
            revision=None,
        )
        saved_item, saved = await _text(repos, set_id, "P1")
    finally:
        release.set()
    status = await task
    assert status["error_code"] == "part_content_rejected" and status["error_message"]
    after_item, after = await _text(repos, set_id, "P1")
    assert after_item == saved_item and after == saved


async def wait_job(service, set_id, pid, job_id):
    for _ in range(200):
        snap = await service.get_workflow(set_id=set_id, product_id=pid, workflow_id=job_id)
        if snap["status"] not in ("queued", "running"):
            return snap
        await asyncio.sleep(0.01)
    pytest.fail("part job did not finish")


@pytest.mark.asyncio
async def test_one_part_only_and_foreign_workflow_is_not_visible():
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    item, before = await _text(repos, set_id, "P1")
    calls = len(llm.prompts)
    started = await service.regenerate_segment(
        set_id=set_id,
        product_id="P1",
        segment_index=1,
        idempotency_key="part1",
        expected_version_id=before.id,
    )
    status = await wait_job(service, set_id, "P1", started["workflow_id"])
    assert status["status"] == "completed" and len(llm.prompts) == calls + 1
    _, after = await _text(repos, set_id, "P1")
    assert after.id != before.id
    assert after.segment_version_ids[0] == before.segment_version_ids[0]
    assert after.segment_version_ids[1] != before.segment_version_ids[1]
    assert after.segment_version_ids[2:] == before.segment_version_ids[2:]
    replay = await service.regenerate_segment(
        set_id=set_id,
        product_id="P1",
        segment_index=1,
        idempotency_key="part1",
        expected_version_id=before.id,
    )
    assert replay["workflow_id"] == started["workflow_id"]
    assert len(llm.prompts) == calls + 1
    with pytest.raises(ScriptAuthoringError) as conflict:
        await service.regenerate_segment(
            set_id=set_id,
            product_id="P1",
            segment_index=2,
            idempotency_key="part1",
            expected_version_id=before.id,
        )
    assert conflict.value.code == "idempotency_conflict"
    other = await _new_set(service)
    with pytest.raises(ScriptAuthoringError):
        await service.get_workflow(
            set_id=other, product_id="P1", workflow_id=started["workflow_id"]
        )
    with pytest.raises(ScriptAuthoringError) as stale:
        await service.regenerate_segment(
            set_id=set_id,
            product_id="P1",
            segment_index=1,
            idempotency_key="stale",
            expected_version_id=before.id,
        )
    assert stale.value.code == "stale_revision"


@pytest.mark.asyncio
async def test_failed_model_keeps_the_displayed_version():
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    item, before = await _text(repos, set_id, "P1")
    llm.empty_for = {("Gel XYZ", "Điểm nổi bật")}
    started = await service.regenerate_segment(
        set_id=set_id,
        product_id="P1",
        segment_index=1,
        idempotency_key="reject",
        expected_version_id=before.id,
    )
    status = await wait_job(service, set_id, "P1", started["workflow_id"])
    assert status["status"] == "failed" and status["error_code"] == "part_content_rejected"
    after_item, after = await _text(repos, set_id, "P1")
    assert after.id == before.id and after_item.state == item.state


@pytest.mark.asyncio
async def test_saving_unchanged_ai_text_preserves_part_provenance():
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    _, before = await _text(repos, set_id, "P1")
    await service.save_draft(
        set_id=set_id,
        product_id="P1",
        display_text=before.display_text,
        spoken_text=None,
        revision=None,
    )
    _, after = await _text(repos, set_id, "P1")
    assert after.id == before.id and after.segment_version_ids == before.segment_version_ids
    await service.submit_for_gate(set_id=set_id, product_id="P1")
    _, checked = await _text(repos, set_id, "P1")
    assert checked.id == before.id and checked.segment_version_ids == before.segment_version_ids


@pytest.mark.asyncio
async def test_part_completion_uses_the_same_transaction_as_selected_version():
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    _, source = await _text(repos, set_id, "P1")
    updates = []
    original = repos.jobs.update

    async def observe(job, **kwargs):
        if job.status.value == "completed":
            updates.append(kwargs.get("conn"))
        await original(job, **kwargs)

    repos.jobs.update = observe
    started = await service.regenerate_segment(
        set_id=set_id,
        product_id="P1",
        segment_index=1,
        idempotency_key="atomic",
        expected_version_id=source.id,
    )
    assert (await wait_job(service, set_id, "P1", started["workflow_id"]))["status"] == "completed"
    assert len(updates) == 1 and updates[0] is not None
