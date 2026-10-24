"""Authoring SQL repository tests (Change B, B2).

RED before ``application/script_authoring/repositories.py`` exists: imports
fail. GREEN once ``PostgresAuthoringRepositories`` implements the interfaces.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.generation.batch import BatchState
from backend.application.script_authoring.models import (
    Approval,
    GateRun,
    GateViolation,
    GenerationBatch,
    GenerationJob,
    GenerationJobStatus,
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


@pytest.mark.asyncio
async def test_model_dispatch_budget_is_atomic_and_survives_reconnect(pg_url: str):
    repos = await _connect(pg_url)
    s = _set()
    await repos.script_sets.insert(s)
    batch = GenerationBatch(id=new_id("batch"), script_set_id=s.id)
    await repos.batches.insert(batch, state=BatchState(batch_id=batch.id, script_set_id=s.id))
    try:
        accepted = await asyncio.gather(
            *(repos.model_budget.reserve(batch.id, "p1", 1) for _ in range(10))
        )
        assert sum(accepted) == 3
    finally:
        await repos.close()
    repos = await _connect(pg_url)
    try:
        assert not await repos.model_budget.reserve(batch.id, "p1", 1)
        assert await repos.model_budget.reserve(batch.id, "p1", 1, operation_id="repair-one")
        assert await repos.model_budget.reserve(batch.id, "p1", 1, operation_id="repair-two")
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_recovered_replacement_then_new_generation_respects_unique_versions(pg_url: str):
    from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl

    repos = await _connect(pg_url)
    engine = SimpleNamespace(
        llm_cfg={"engine": "fake"}, llm_failed=False, get_llm_fn=lambda **kwargs: lambda prompt: ""
    )
    service = ScriptAuthoringServiceImpl(repos, engine_manager=engine)
    try:
        created = await service.create_script_set(
            name="proof",
            transition_policy="ORDER_AWARE",
            product_ids=["p1"],
            brief={
                "product_facts": {
                    "p1": {
                        "product_name": "Áo denim",
                        "prices": ["180000 VND"],
                        "allowed_claims": [
                            "Chất vải denim dày dặn.",
                            "Giao hàng toàn quốc từ hai đến ba ngày.",
                        ],
                    }
                }
            },
        )
        set_id = created["id"]

        async def start(key):
            return await service.start_batch_generation(
                set_id=set_id,
                product_ids=[],
                target_duration_s=60,
                idempotency_key=key,
                ordered_units=True,
                replace_existing=True,
            )

        await start("first")
        await asyncio.gather(*service._tasks)
        original = await repos.items.get_by_product(set_id, "p1")
        assert original.current_version_id is not None
        spawn = service._spawn
        service._spawn = lambda coro, **kwargs: coro.close()
        batch_id = (await start("recovery"))["batch_id"]
        orch = service._active_orchestrators[batch_id]
        for _ in range(2):
            await asyncio.to_thread(orch.step)
        revisions = await service._current_item_revisions(batch_id)
        await service._drain_artifacts(service._batch_artifact_bridges[batch_id], revisions, set())
        await service._drain_batch_persists(batch_id, service._batch_persist_queues[batch_id])
        recovered, queue, bridge = await service._recover_orchestrator(batch_id, set_id)
        existing = {v.id for v in await repos.versions.list_by_item(original.id)}
        await service._run_batch_job(batch_id, recovered, queue, bridge, revisions, existing)
        versions = await repos.versions.list_by_item(original.id)
        assert [v.version for v in versions] == [1, 2]
        service._spawn = spawn
        await start("third")
        await asyncio.gather(*service._tasks)
        assert [v.version for v in await repos.versions.list_by_item(original.id)] == [1, 2, 3]
        # Terminal receipt and new text must roll back together if the job
        # update fails, and a successful receipt must survive recovery/replay.
        item = await repos.items.get(original.id)
        source = await repos.versions.get(item.current_version_id)
        segment = await repos.segments.get(source.segment_version_ids[2])
        calls = []

        def write(prompt):
            calls.append(prompt)
            return segment.spoken_text

        engine.get_llm_fn = lambda **kwargs: write
        update = repos.jobs.update

        async def fail_terminal(job, *, conn=None, **kwargs):
            await update(job, conn=conn, **kwargs)
            if job.status is GenerationJobStatus.COMPLETED:
                assert conn is not None
                raise ConnectionError("injected before commit")

        repos.jobs.update = fail_terminal
        failed = await service.regenerate_segment(
            set_id=set_id,
            product_id="p1",
            segment_index=segment.segment_index,
            idempotency_key="atomic-fail",
            expected_version_id=source.id,
        )
        await asyncio.gather(*service._tasks)
        assert (await repos.jobs.get(failed["workflow_id"])).status is GenerationJobStatus.FAILED
        assert (await repos.items.get(original.id)).current_version_id == source.id
        assert len(await repos.versions.list_by_item(original.id)) == 3
        repos.jobs.update = update
        request = dict(
            set_id=set_id,
            product_id="p1",
            segment_index=segment.segment_index,
            idempotency_key="atomic-success",
            expected_version_id=source.id,
        )
        succeeded = await service.regenerate_segment(**request)
        await asyncio.gather(*service._tasks)
        completed = await repos.jobs.get(succeeded["workflow_id"])
        assert completed.status is GenerationJobStatus.COMPLETED
        count = len(calls)
        await service._resume_job(completed)
        assert (await service.regenerate_segment(**request))["workflow_id"] == completed.id
        assert len(calls) == count and count >= 2
    finally:
        await service.drain()
        await repos.close()


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
async def test_plan_briefs_and_segment_provenance_roundtrip(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id)
        await repos.items.insert(item)
        briefs = {"schema_version": 1, "parts": [{"part_id": "part:1", "role": "highlight"}]}
        plan = ProductScriptPlan(
            id=new_id("plan"),
            script_item_id=item.id,
            product_id="p1",
            target_duration_s=600,
            K=1,
            unit_briefs=briefs,
        )
        await repos.plans.insert(plan)
        fallback = ScriptSegment(
            id=new_id("segment"),
            script_item_id=item.id,
            plan_id=plan.id,
            segment_index=0,
            spoken_text="a",
            part_id="part:1",
            provenance={"source": "fallback", "fallback_reason_code": "coverage_missing"},
        )
        legacy = ScriptSegment(
            id=new_id("segment"),
            script_item_id=item.id,
            plan_id=plan.id,
            segment_index=0,
            version=2,
            spoken_text="b",
        )
        await repos.segments.insert(fallback)
        await repos.segments.insert(legacy)
        loaded = await repos.plans.get(plan.id)
        assert loaded is not None and loaded.unit_briefs == briefs
        got = await repos.segments.get(fallback.id)
        assert (
            got.part_id == "part:1" and got.provenance["fallback_reason_code"] == "coverage_missing"
        )
        old = await repos.segments.get(legacy.id)
        assert old.part_id is None and old.provenance is None
        assert (await repos.plans.get_latest(item.id)).segments[0].id == legacy.id
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_concurrent_segment_version_is_reported_as_a_lost_race(pg_url: str) -> None:
    from backend.application.script_authoring.repositories import StaleRevisionError

    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id)
        await repos.items.insert(item)
        plan = ProductScriptPlan(
            id=new_id("plan"), script_item_id=item.id, product_id="p1", target_duration_s=60, K=1
        )
        await repos.plans.insert(plan)

        def seg() -> ScriptSegment:
            return ScriptSegment(
                id=new_id("segment"),
                script_item_id=item.id,
                plan_id=plan.id,
                segment_index=0,
                version=2,
                spoken_text="x",
            )

        await repos.segments.insert(seg())
        with pytest.raises(StaleRevisionError):
            await repos.segments.insert(seg())
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_item_lock_serializes_two_concurrent_saves_and_version_clash_is_stale(
    pg_url: str,
) -> None:
    import asyncio

    from backend.application.script_authoring.repositories import StaleRevisionError

    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        item = _item(s.id)
        await repos.items.insert(item)
        first_holds_lock = asyncio.Event()
        release_first = asyncio.Event()
        seen: dict[str, int | None] = {}

        async def winner() -> None:
            async with repos.transaction() as conn:
                seen["winner"] = await repos.items.lock_revision(item.id, conn=conn)
                first_holds_lock.set()
                await release_first.wait()
                await repos.items.update(item, expected_revision=0, conn=conn)

        async def loser() -> None:
            await first_holds_lock.wait()
            async with repos.transaction() as conn:
                # blocks on the row lock until the winner commits, then sees its revision
                seen["loser"] = await repos.items.lock_revision(item.id, conn=conn)

        tasks = [asyncio.create_task(winner()), asyncio.create_task(loser())]
        await asyncio.sleep(0.3)
        assert "loser" not in seen  # still waiting for the lock
        release_first.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
        assert seen == {"winner": 0, "loser": 1}

        v = ScriptVersion(
            id=new_id("script_version"), script_item_id=item.id, version=1, spoken_text="a"
        )
        await repos.versions.insert(v)
        clash = ScriptVersion(
            id=new_id("script_version"), script_item_id=item.id, version=1, spoken_text="b"
        )
        with pytest.raises(StaleRevisionError):
            await repos.versions.insert(clash)
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


# ── C10 coverage additions ──────────────────────────────────────────────────


async def _seed_item(repos, product_id: str = "p1") -> ScriptItem:
    s = _set()
    await repos.script_sets.insert(s)
    item = _item(s.id, product_id)
    await repos.items.insert(item)
    return item


async def _seed_version(repos, item: ScriptItem, version: int = 1) -> ScriptVersion:
    v = ScriptVersion(
        id=new_id("script_version"),
        script_item_id=item.id,
        version=version,
        spoken_text=f"v{version}",
    )
    await repos.versions.insert(v)
    return v


@pytest.mark.asyncio
async def test_item_list_by_set_and_get_missing(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        i1 = _item(s.id, "p1")
        i2 = _item(s.id, "p2")
        await repos.items.insert(i1)
        await repos.items.insert(i2)
        assert len(await repos.items.list_by_set(s.id)) == 2
        assert await repos.items.get(new_id("script_item")) is None
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_plan_get_missing_and_get_latest_none(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        assert await repos.plans.get(new_id("plan")) is None
        assert await repos.plans.get_latest(new_id("script_item")) is None
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_segments_insert_get_list_and_selected(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        item = await _seed_item(repos)
        plan = ProductScriptPlan(
            id=new_id("plan"),
            script_item_id=item.id,
            product_id="p1",
            target_duration_s=600,
            K=1,
        )
        await repos.plans.insert(plan)  # script_segments.plan_id FK needs the plan row
        seg = ScriptSegment(
            id=new_id("segment"),
            script_item_id=item.id,
            plan_id=plan.id,
            segment_index=0,
            title="s0",
            spoken_text="a",
        )
        await repos.segments.insert(seg)
        loaded = await repos.segments.get(seg.id)
        assert loaded is not None and loaded.title == "s0"
        assert [s.id for s in await repos.segments.list_by_plan(plan.id)] == [seg.id]
        assert [s.id for s in await repos.segments.list_selected([seg.id])] == [seg.id]
        assert await repos.segments.list_selected([]) == []
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_version_get_missing_and_approved_none(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        item = await _seed_item(repos)
        assert await repos.versions.get(new_id("script_version")) is None
        # No approved pointer yet -> the JOIN yields no row.
        assert await repos.versions.get_approved(item.id) is None
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_gate_runs_list_and_latest_for_version(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        item = await _seed_item(repos)
        version = await _seed_version(repos, item)
        run = GateRun(
            id=new_id("gate_run"),
            script_item_id=item.id,
            passed=True,
            script_version_id=version.id,
        )
        await repos.gate_runs.insert(run)
        assert [r.id for r in await repos.gate_runs.list_by_item(item.id)] == [run.id]
        latest = await repos.gate_runs.latest_for_version(version.id)
        assert latest is not None and latest.id == run.id
        assert await repos.gate_runs.latest_for_version(new_id("script_version")) is None
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_approval_get_and_none_paths(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        item = await _seed_item(repos)
        version = await _seed_version(repos, item)
        run = GateRun(id=new_id("gate_run"), script_item_id=item.id, passed=True)
        await repos.gate_runs.insert(run)  # script_approvals.gate_run_id FK needs this row
        approval = Approval(
            id=new_id("approval"),
            script_item_id=item.id,
            script_version_id=version.id,
            actor="operator",
            approval_hash="h",
            gate_run_id=run.id,
        )
        await repos.approvals.insert(approval, dependencies={"rule_set_version": "rs1"})
        loaded = await repos.approvals.get(approval.id)
        assert loaded is not None and loaded.id == approval.id
        assert await repos.approvals.get(new_id("approval")) is None
        assert await repos.approvals.get_by_item(new_id("script_item")) is None
        assert await repos.approvals.recorded_dependencies(new_id("script_item")) == {}
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_batch_find_by_idempotency(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        s = _set()
        await repos.script_sets.insert(s)
        batch = GenerationBatch(id=new_id("batch"), script_set_id=s.id, idempotency_key="key-b")
        await repos.batches.insert(batch, state=BatchState(batch_id=batch.id, script_set_id=s.id))
        found = await repos.batches.find_by_idempotency(s.id, "key-b")
        assert found is not None and found.id == batch.id
        assert await repos.batches.find_by_idempotency(s.id, "") is None
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_jobs_list_update_and_idempotency_empty_key(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        item = await _seed_item(repos)
        batch = GenerationBatch(id=new_id("batch"), script_set_id=item.script_set_id)
        await repos.batches.insert(
            batch, state=BatchState(batch_id=batch.id, script_set_id=item.script_set_id)
        )
        job = GenerationJob(
            id=new_id("job"),
            batch_id=batch.id,
            script_item_id=item.id,
            product_id="p1",
            intent="generate_long_form",
            target_duration_s=600,
            idempotency_key="key-j",
        )
        await repos.jobs.insert(job)
        assert [j.id for j in await repos.jobs.list_by_batch(batch.id)] == [job.id]
        assert await repos.jobs.find_by_idempotency(item.id, "generate_long_form", "") is None
        job.status = GenerationJobStatus.RUNNING
        job.attempt_count = 1
        await repos.jobs.update(job)
        loaded = await repos.jobs.get(job.id)
        assert loaded is not None and loaded.status is GenerationJobStatus.RUNNING
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_item_update_stale_revision(pg_url: str) -> None:
    from backend.application.script_authoring.repositories import StaleRevisionError

    repos = await _connect(pg_url)
    try:
        item = await _seed_item(repos)
        await repos.items.update(item, expected_revision=0)
        with pytest.raises(StaleRevisionError):
            await repos.items.update(item, expected_revision=0)
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_repo_methods_with_explicit_conn(pg_url: str) -> None:
    """Passing ``conn`` into repo methods exercises the owned-conn branches."""
    repos = await _connect(pg_url)
    try:
        item = await _seed_item(repos)
        plan = ProductScriptPlan(
            id=new_id("plan"),
            script_item_id=item.id,
            product_id="p1",
            target_duration_s=600,
            K=1,
        )
        seg = ScriptSegment(
            id=new_id("segment"),
            script_item_id=item.id,
            plan_id=plan.id,
            segment_index=0,
            title="s0",
            spoken_text="a",
        )
        run = GateRun(id=new_id("gate_run"), script_item_id=item.id, passed=True)
        async with repos.transaction() as conn:
            await repos.plans.insert(plan, conn=conn)  # segment plan_id FK needs the plan row
            await repos.segments.insert(seg, conn=conn)
            loaded = await repos.segments.get(seg.id, conn=conn)
            assert loaded is not None and loaded.title == "s0"
            assert [s.id for s in await repos.segments.list_by_plan(plan.id, conn=conn)] == [seg.id]
            await repos.gate_runs.insert(run, conn=conn)
            assert [r.id for r in await repos.gate_runs.list_by_item(item.id, conn=conn)] == [
                run.id
            ]
            assert len(await repos.items.list_by_set(item.script_set_id, conn=conn)) == 1
    finally:
        await repos.close()
