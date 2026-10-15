"""A part repair must not replace siblings or publish rejected content."""

import asyncio
import pytest
from test_ordered_generation import _service, _new_set, _run, FakeLLM, _text
from backend.application.script_authoring.service import ScriptAuthoringError


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
