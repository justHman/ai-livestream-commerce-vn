import asyncio
import pytest
from test_ordered_generation import _service, _new_set, _run, _text, FakeLLM
from test_service_impl_generation import _fail_result


async def replace(service, repos, set_id, key="replace"):
    started = await service.start_batch_generation(
        set_id=set_id,
        product_ids=[],
        target_duration_s=60,
        idempotency_key=key,
        ordered_units=True,
        replace_existing=True,
    )
    for _ in range(300):
        snap = await service.get_batch(set_id=set_id, batch_id=started["batch_id"])
        if snap["outcome"] != "running":
            return snap
        await asyncio.sleep(0.01)
    pytest.fail("replacement did not finish")


@pytest.mark.asyncio
async def test_whole_replacement_keeps_set_and_immutable_previous_version():
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    _, before = await _text(repos, set_id, "P1")
    old_text = before.spoken_text
    snap = await replace(service, repos, set_id)
    assert snap["outcome"] == "succeeded"
    _, after = await _text(repos, set_id, "P1")
    assert after.id != before.id and after.version == before.version + 1
    assert (await repos.versions.get(before.id)).spoken_text == old_text
    assert len(repos.script_sets.rows) == 1


@pytest.mark.asyncio
async def test_failed_whole_replacement_leaves_previous_script_selected(monkeypatch):
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    old = {pid: await _text(repos, set_id, pid) for pid in ("P1", "P2")}
    monkeypatch.setattr(service._gate, "run_full_script", lambda *args: _fail_result())
    snap = await replace(service, repos, set_id)
    assert snap["outcome"] == "failed"
    assert all(p["kept_previous_version"] for p in snap["products"])
    for pid, (before_item, before) in old.items():
        item, after = await _text(repos, set_id, pid)
        assert item.state == before_item.state and after.id == before.id


@pytest.mark.asyncio
async def test_replacement_recovery_restores_history_once_and_can_generate_again():
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    spawn = service._spawn
    service._spawn = lambda coro, **kw: coro.close()
    started = await service.start_batch_generation(
        set_id=set_id,
        product_ids=[],
        target_duration_s=60,
        idempotency_key="recover",
        ordered_units=True,
        replace_existing=True,
    )
    batch_id = started["batch_id"]
    orch = service._active_orchestrators[batch_id]
    # Persist replacement after planning, as a worker would before a restart.
    for _ in range(2):
        await asyncio.to_thread(orch.step)
    revisions = await service._current_item_revisions(batch_id)
    await service._drain_artifacts(service._batch_artifact_bridges[batch_id], revisions, set())
    await service._drain_batch_persists(batch_id, service._batch_persist_queues[batch_id])
    recovered, queue, bridge = await service._recover_orchestrator(batch_id, set_id)
    for driver in recovered.workflows.values():
        history = driver._driver.workflow.versions
        assert len(history) == len({v.id for v in history}) == 1
    existing = {v.id for v in repos.versions.rows.values()}
    await service._run_batch_job(batch_id, recovered, queue, bridge, revisions, existing)
    _, after = await _text(repos, set_id, "P1")
    assert after.version == 2
    service._spawn = spawn
    assert (await replace(service, repos, set_id, key="after-recovery"))["outcome"] == "succeeded"
    _, last = await _text(repos, set_id, "P1")
    assert last.version == 3
