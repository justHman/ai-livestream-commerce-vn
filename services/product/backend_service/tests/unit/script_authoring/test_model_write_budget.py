import pytest

from test_ordered_generation import FakeLLM, _new_set, _run, _service, _text
from test_regenerate_part import wait_job


class DurableBudget:
    def __init__(self):
        self.writes = {}

    async def reserve(self, batch, product, index, *, operation_id="full"):
        key = batch, product, index, operation_id
        used = self.writes.get(key, 0)
        if used >= 3:
            return False
        self.writes[key] = used + 1
        return True


@pytest.mark.asyncio
async def test_full_and_part_operations_have_separate_durable_dispatch_budgets():
    llm = FakeLLM()
    service, repos = _service(llm)
    repos.model_budget = DurableBudget()
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    # The complete-session style pass may spend one additional write on a
    # repeated opener, inside the same original budget (never a new operation).
    assert repos.model_budget.writes and all(
        1 <= v <= 2 for v in repos.model_budget.writes.values()
    )
    # A user can repair the same part in two distinct operations. A full
    # generation's reservation must not consume either repair's allowance.
    for nonce in ("repair-one", "repair-two"):
        _, source = await _text(repos, set_id, "P1")
        started = await service.regenerate_segment(
            set_id=set_id,
            product_id="P1",
            segment_index=1,
            idempotency_key=nonce,
            expected_version_id=source.id,
        )
        assert (await wait_job(service, set_id, "P1", started["workflow_id"]))[
            "status"
        ] == "completed"
    assert len({key[3] for key in repos.model_budget.writes}) == 3


@pytest.mark.asyncio
async def test_recovery_cannot_reset_a_consumed_part_budget():
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await _run(service, repos, set_id)
    _, source = await _text(repos, set_id, "P1")
    # Capture dispatch, then simulate the original worker having spent its three
    # requests before restarting, without changing the selected script version.
    queued = []

    def capture(coro, **kwargs):
        coro.close()
        queued.append(kwargs)

    service._spawn = capture
    start = await service.regenerate_segment(
        set_id=set_id,
        product_id="P1",
        segment_index=1,
        idempotency_key="recover",
        expected_version_id=source.id,
    )
    job = await repos.jobs.get(start["workflow_id"])
    repos.model_budget = DurableBudget()
    repos.model_budget.writes[job.batch_id, "P1", 1, job.id] = 3
    calls = len(llm.prompts)
    await service._resume_job(job)
    assert job.error_code == "part_model_budget_exhausted"
    assert len(llm.prompts) == calls
    _, after = await _text(repos, set_id, "P1")
    assert after.id == source.id
