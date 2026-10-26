import asyncio
import threading
import pytest

from test_model_write_budget import DurableBudget
from test_ordered_generation import FakeLLM, _new_set, _run, _service, _text

from backend.application.script_authoring.generation.script_variety import audit, repair_budget
from backend.application.script_authoring.generation.variety_finalizer import (
    finalize,
    selected_parts,
)
from backend.application.script_authoring.service_impl import _SyncPersistBridge


class RepairingLLM(FakeLLM):
    def __init__(self, bad=False):
        super().__init__()
        self.bad = bad
        self.repairs = 0

    def __call__(self, prompt):
        text = super().__call__(prompt)
        if "Chỉ đổi tối đa sáu từ dẫn trung tính" in prompt:
            self.repairs += 1
            return (
                "Miễn phí toàn bộ sản phẩm cho mọi người."
                if self.bad
                else text.replace("Giá chỉ", "Mức giá là")
            )
        return text


@pytest.mark.asyncio
async def test_complete_batch_repairs_only_repeated_ai_part_before_done():
    llm = RepairingLLM()
    service, repos = _service(llm)
    repos.model_budget = DurableBudget()
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    state = repos.batches.rows[batch_id][1]
    assert state.status == "completed" and state.variety["done"]
    assert state.variety["initial_excess"] > state.variety["remaining_excess"]
    assert llm.repairs == state.variety["calls"] == 1
    parts, _ = await selected_parts(service, await repos.script_sets.get(set_id))
    assert all(g.prefix == "bạn nào quan" for g in audit(parts).groups)
    assert state.variety["calls"] <= repair_budget(len(parts))
    assert all(v <= 2 for v in repos.model_budget.writes.values())
    _first, before = await _text(repos, set_id, "P2")
    _last, after = await _text(repos, set_id, "P1")
    assert before.version == 1 and after.version == 2
    assert "Giá chỉ một trăm nghìn đồng" in before.spoken_text
    assert "Mức giá là" in after.spoken_text and "một trăm năm mươi nghìn đồng" in after.spoken_text
    assert (
        state.actual_semantic_calls
        == sum(p.semantic_calls for p in state.products.values()) + llm.repairs
    )


@pytest.mark.asyncio
async def test_invalid_repair_keeps_valid_script_without_fallback_or_retry():
    llm = RepairingLLM(bad=True)
    service, repos = _service(llm)
    repos.model_budget = DurableBudget()
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    state = repos.batches.rows[batch_id][1]
    assert state.status == "completed" and state.variety["done"]
    assert llm.repairs == 1 and state.variety["remaining_excess"] > 0
    for pid in ("P2", "P1"):
        _, version = await _text(repos, set_id, pid)
        assert version.version == 1 and "Miễn phí" not in version.spoken_text


@pytest.mark.asyncio
async def test_recovery_does_not_repeat_a_persisted_style_attempt():
    llm = RepairingLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    state = repos.batches.rows[batch_id][1]
    parts, _ = await selected_parts(service, await repos.script_sets.get(set_id))
    from backend.application.script_authoring.generation.script_variety import repair_candidates

    candidate = repair_candidates(parts, audit(parts))[0]
    state.variety = {"attempted": [candidate.key], "calls": 1, "limit": repair_budget(len(parts))}
    state.status = "running"
    repos.model_budget = DurableBudget()
    original_calls = len(llm.prompts)
    await finalize(service, state, _SyncPersistBridge(), {}, set())
    assert len(llm.prompts) == original_calls
    assert state.variety["done"] and not repos.model_budget.writes


@pytest.mark.asyncio
async def test_human_edited_part_is_audit_only():
    llm = RepairingLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    _, source = await _text(repos, set_id, "P1")
    for sid in source.segment_version_ids:
        segment = await repos.segments.get(sid)
        segment.provenance = {"source": "manual"}
    state = repos.batches.rows[batch_id][1]
    state.variety, state.status = {}, "running"
    repos.model_budget = DurableBudget()
    calls = len(llm.prompts)
    await finalize(service, state, _SyncPersistBridge(), {}, set())
    assert len(llm.prompts) == calls and not repos.model_budget.writes
    assert (await _text(repos, set_id, "P1"))[1].id == source.id


@pytest.mark.asyncio
async def test_new_batch_does_not_reset_old_parts_write_budget():
    llm = RepairingLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    _, source = await _text(repos, set_id, "P1")
    repos.model_budget = DurableBudget()
    calls = len(llm.prompts)
    batch_id = await _run(service, repos, set_id, key="new-batch-old-parts")
    state = repos.batches.rows[batch_id][1]
    assert state.status == "completed" and state.variety["done"]
    assert state.variety["remaining_excess"] > 0
    assert len(llm.prompts) == calls and not repos.model_budget.writes
    assert (await _text(repos, set_id, "P1"))[1].id == source.id


@pytest.mark.asyncio
async def test_shutdown_cancels_finalization_before_releasing_lease(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockedRepair(RepairingLLM):
        def __call__(self, prompt):
            if "Chỉ đổi tối đa sáu từ dẫn trung tính" in prompt:
                started.set()
                assert release.wait(5), "test did not release the controlled provider"
                try:
                    return super().__call__(prompt)
                finally:
                    finished.set()
            return super().__call__(prompt)

    service, repos = _service(BlockedRepair())
    repos.model_budget = DurableBudget()
    set_id = await _new_set(service)
    writes = []
    original = repos.batches.update_state

    async def record(*args, **kwargs):
        writes.append((kwargs.get("lease_owner"), kwargs.get("lease_epoch")))
        return await original(*args, **kwargs)

    monkeypatch.setattr(repos.batches, "update_state", record)
    try:
        await service.start_batch_generation(
            set_id=set_id,
            product_ids=[],
            target_duration_s=60,
            idempotency_key="shutdown",
            ordered_units=True,
        )
        assert await asyncio.to_thread(started.wait, 3)
        await service.drain(timeout_s=0)
        writes_at_shutdown = len(writes)
        assert not service._tasks and not service._leased_batches
        assert writes and all(owner is not None and epoch is not None for owner, epoch in writes)
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        await asyncio.sleep(0)  # flush the cancelled provider's late completion callback
        assert len(writes) == writes_at_shutdown
    finally:
        release.set()
        await service.drain(timeout_s=0)


@pytest.mark.asyncio
async def test_checkpoint_never_falls_back_to_unfenced_write(monkeypatch):
    from backend.application.script_authoring.generation.variety_finalizer import checkpoint
    from backend.application.script_authoring.repositories import LeaseLostError

    service, repos = _service(RepairingLLM())
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    state = (await repos.batches.get(batch_id))[1]
    captured = service._batch_lease(batch_id)
    service._leased_batches.clear()
    with pytest.raises(LeaseLostError):
        await checkpoint(service, state, None)
    original = repos.batches.update_state

    async def require_captured(*args, **kwargs):
        assert (kwargs["lease_owner"], kwargs["lease_epoch"]) == captured
        return await original(*args, **kwargs)

    monkeypatch.setattr(repos.batches, "update_state", require_captured)
    await checkpoint(service, state, captured)
