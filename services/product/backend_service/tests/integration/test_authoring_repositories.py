"""Authoring SQL repository tests (Change B, B2).

RED before ``application/script_authoring/repositories.py`` exists: imports
fail. GREEN once ``PostgresAuthoringRepositories`` implements the interfaces.
"""

from __future__ import annotations

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.generation.batch import BatchState
from backend.application.script_authoring.models import (
    Approval,
    GateRun,
    GateViolation,
    GenerationBatch,
    GenerationJob,
    ProductScriptPlan,
    ScriptItem,
    ScriptSegment,
    ScriptSet,
    ScriptState,
    ScriptVersion,
    new_id,
)


async def _connect(pg_url: str):
    from backend.application.script_authoring.repositories import (
        PostgresAuthoringRepositories,
    )

    store = PostgresRuntimeStore(pg_url)
    await store.connect()
    await store.apply_schema()
    repos = PostgresAuthoringRepositories(pg_url)
    await repos.connect()
    return repos


def _set(title: str = "t") -> ScriptSet:
    return ScriptSet(id=new_id("script_set"), shop_id="shop1", title=title)


def _item(set_id: str, product_id: str = "p1") -> ScriptItem:
    return ScriptItem(id=new_id("script_item"), script_set_id=set_id, product_id=product_id)


@pytest.mark.asyncio
async def test_script_set_insert_get_update(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set("my set")
        await repos.script_sets.insert(s)
        loaded = await repos.script_sets.get(s.id)
        assert loaded is not None and loaded.title == "my set"

        s.title = "renamed"
        await repos.script_sets.update(s, expected_revision=0)
        loaded = await repos.script_sets.get(s.id)
        assert loaded is not None and loaded.title == "renamed" and loaded.revision == 1
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_script_set_update_stale_revision(pg_url: str) -> None:
    from backend.application.script_authoring.repositories import StaleRevisionError

    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        s.title = "v2"
        await repos.script_sets.update(s, expected_revision=0)
        s.title = "v3"
        with pytest.raises(StaleRevisionError):
            await repos.script_sets.update(s, expected_revision=0)
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_script_item_by_product_and_update(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id, "p1")
        await repos.items.insert(item)
        found = await repos.items.get_by_product(s.id, "p1")
        assert found is not None and found.id == item.id
        assert await repos.items.get_by_product(s.id, "missing") is None

        item.state = ScriptState.DRAFT
        await repos.items.update(item, expected_revision=0)
        found = await repos.items.get(item.id)
        assert found is not None and found.state == "draft"
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_plan_and_segments_roundtrip(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id)
        await repos.items.insert(item)
        plan = ProductScriptPlan(
            id=new_id("plan"),
            script_item_id=item.id,
            product_id="p1",
            target_duration_s=600,
            K=2,
            segments=[
                ScriptSegment(
                    id=new_id("segment"),
                    script_item_id=item.id,
                    plan_id="placeholder",
                    segment_index=0,
                    title="s0",
                    spoken_text="a",
                ),
                ScriptSegment(
                    id=new_id("segment"),
                    script_item_id=item.id,
                    plan_id="placeholder",
                    segment_index=1,
                    title="s1",
                    spoken_text="b",
                ),
            ],
        )
        plan.segments[0].plan_id = plan.id
        plan.segments[1].plan_id = plan.id
        await repos.plans.insert(plan)
        loaded = await repos.plans.get(plan.id)
        assert loaded is not None and loaded.segment_count == 2
        assert [seg.segment_index for seg in loaded.segments] == [0, 1]
        latest = await repos.plans.get_latest(item.id)
        assert latest is not None and latest.id == plan.id
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_version_immutable_and_get_approved(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id)
        await repos.items.insert(item)
        v1 = ScriptVersion(
            id=new_id("script_version"), script_item_id=item.id, version=1, spoken_text="hello"
        )
        v2 = ScriptVersion(
            id=new_id("script_version"), script_item_id=item.id, version=2, spoken_text="world"
        )
        await repos.versions.insert(v1)
        await repos.versions.insert(v2)
        assert (await repos.versions.get(v1.id)).spoken_text == "hello"
        assert len(await repos.versions.list_by_item(item.id)) == 2
        assert await repos.versions.get_approved(item.id) is None

        item.approved_version_id = v2.id
        await repos.items.update(item, expected_revision=0)
        approved = await repos.versions.get_approved(item.id)
        assert approved is not None and approved.id == v2.id
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_gate_run_roundtrip(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id)
        await repos.items.insert(item)
        run = GateRun(
            id=new_id("gate_run"),
            script_item_id=item.id,
            full=False,
            passed=False,
            violations=[GateViolation(rule_id="format.vn", severity="error", message="bad")],
        )
        await repos.gate_runs.insert(run)
        loaded = await repos.gate_runs.get(run.id)
        assert loaded is not None and loaded.passed is False
        assert len(loaded.violations) == 1 and loaded.violations[0].rule_id == "format.vn"
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_approval_with_recorded_dependencies(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id)
        await repos.items.insert(item)
        version = ScriptVersion(id=new_id("script_version"), script_item_id=item.id, version=1)
        await repos.versions.insert(version)
        run = GateRun(id=new_id("gate_run"), script_item_id=item.id, passed=True)
        await repos.gate_runs.insert(run)
        approval = Approval(
            id=new_id("approval"),
            script_item_id=item.id,
            script_version_id=version.id,
            actor="operator",
            approval_hash="h",
            gate_run_id=run.id,
        )
        deps = {"rule_set_version": "rs1", "product_facts_version": "pf1"}
        await repos.approvals.insert(approval, dependencies=deps)
        loaded = await repos.approvals.get_by_item(item.id)
        assert loaded is not None and loaded.id == approval.id
        recorded = await repos.approvals.recorded_dependencies(item.id)
        assert recorded["rule_set_version"] == "rs1"
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_batch_state_roundtrip_and_revision_guard(pg_url: str) -> None:
    from backend.application.script_authoring.repositories import StaleRevisionError

    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        state = BatchState(batch_id=new_id("batch"), script_set_id=s.id)
        state.bump_revision()  # revision 1
        batch = GenerationBatch(id=state.batch_id, script_set_id=s.id)
        await repos.batches.insert(batch, state=state)

        loaded, loaded_state = await repos.batches.get(state.batch_id)
        assert loaded is not None and loaded_state.revision == 1
        assert loaded_state.script_set_id == s.id

        loaded_state.bump_revision()  # revision 2
        await repos.batches.update_state(state.batch_id, state=loaded_state, expected_revision=1)
        with pytest.raises(StaleRevisionError):
            await repos.batches.update_state(
                state.batch_id, state=loaded_state, expected_revision=1
            )
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_job_idempotency_lookup(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id)
        await repos.items.insert(item)
        batch = GenerationBatch(id=new_id("batch"), script_set_id=s.id)
        await repos.batches.insert(batch, state=BatchState(batch_id=batch.id, script_set_id=s.id))
        job = GenerationJob(
            id=new_id("job"),
            batch_id=batch.id,
            script_item_id=item.id,
            product_id="p1",
            intent="generate_long_form",
            target_duration_s=600,
            idempotency_key="key-x",
        )
        await repos.jobs.insert(job)
        found = await repos.jobs.find_by_idempotency(item.id, "generate_long_form", "key-x")
        assert found is not None and found.id == job.id
        assert await repos.jobs.find_by_idempotency(item.id, "generate_long_form", "other") is None
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_idempotency_registry_first_wins(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        await repos.idempotency.register("fp-1", "batch-1")
        await repos.idempotency.register("fp-1", "batch-2")
        assert await repos.idempotency.get("fp-1") == "batch-1"
        assert await repos.idempotency.get("missing") is None
    finally:
        await repos.close()
