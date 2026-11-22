"""AI writes cannot inherit the owner's advisory full-script claim policy."""

from types import SimpleNamespace

import pytest
from test_gate_relaxed_claims import _gate
from test_service_impl_generation import _FakeGate, _fail_result, _make_service

from backend.application.script_authoring.models import (
    GenerationJob,
    GenerationJobStatus,
    ScriptSegment,
    ScriptState,
    new_id,
)

BAD_TEXT = "Giá chỉ 99.000đ, chữa khỏi mụn"


class _RecordingGate:
    def __init__(self):
        self.gate = _gate()
        self.segment_results = []

    def run_segment(self, text, context):
        result = self.gate.run_segment(text, context)
        self.segment_results.append((text, result))
        return result

    def run_full_script(self, segments, context):
        return self.gate.run_full_script(segments, context)


def _assert_bad_claims_checked(service):
    results = [result for text, result in service._gate.segment_results if text == BAD_TEXT]
    assert len(results) == 1
    assert {v.rule_id for v in results[0].violations if v.severity.value == "error"} >= {
        "CLAIM_PRICE",
        "CLAIM_FACTUAL",
    }


async def _setup():
    service, repos = _make_service(_FakeGate(_fail_result()))
    created = await service.create_script_set(
        name="Phiên",
        transition_policy="ORDER_AGNOSTIC",
        product_ids=["P1"],
        brief={"product_facts": {"P1": {"prices": ["299.000đ"]}}},
    )
    await service.save_draft(
        set_id=created["id"],
        product_id="P1",
        display_text="Bản hiện tại.",
        spoken_text="Bản hiện tại.",
        revision=None,
    )
    await service.submit_for_gate(set_id=created["id"], product_id="P1")
    item = await repos.items.get_by_product(created["id"], "P1")
    script_set = await repos.script_sets.get(created["id"])
    source = repos.versions.rows[item.current_version_id]
    job = GenerationJob(
        id=new_id("job"),
        batch_id=new_id("batch"),
        script_item_id=item.id,
        product_id="P1",
        target_duration_s=60,
        status=GenerationJobStatus.RUNNING,
    )
    await repos.jobs.insert(job)
    service._gate = _RecordingGate()
    return service, repos, item, script_set, source, job


@pytest.mark.asyncio
async def test_legacy_regenerate_rejects_strict_segment_claims(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    service, repos, item, script_set, source, job = await _setup()
    plan = SimpleNamespace(
        id=new_id("plan"),
        script_item_id=item.id,
        target_duration_s=60,
    )
    await repos.plans.insert(plan)
    segment = ScriptSegment(
        id=new_id("segment"),
        script_item_id=item.id,
        plan_id=plan.id,
        segment_index=0,
        target_duration_s=30,
        display_text=source.display_text,
        spoken_text=source.spoken_text,
    )
    await repos.segments.insert(segment)
    versions_before = set(repos.versions.rows)
    await service._run_regenerate_job(job, item, script_set, 0, lambda _: BAD_TEXT)
    _assert_bad_claims_checked(service)
    assert job.status is GenerationJobStatus.FAILED
    assert job.error_code == "part_content_rejected"
    assert set(repos.versions.rows) == versions_before
    assert set(repos.segments.rows) == {segment.id}
    stored = await repos.items.get(item.id)
    assert stored.current_version_id == source.id
    assert stored.state is ScriptState.GATE_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [BAD_TEXT, "Xin chào mọi người.\n\n" + BAD_TEXT])
async def test_fix_job_rejects_claim_errors_in_each_unit(monkeypatch, fixed):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    service, repos, item, script_set, source, job = await _setup()
    versions_before = set(repos.versions.rows)
    await service._run_fix_job(job, item, script_set, source, lambda _: fixed)
    _assert_bad_claims_checked(service)
    assert job.status is GenerationJobStatus.FAILED
    assert set(repos.versions.rows) == versions_before
    stored = await repos.items.get(item.id)
    assert stored.current_version_id == source.id
    assert stored.state is ScriptState.FAILED
    assert repos.versions.rows[source.id].spoken_text == "Bản hiện tại."
