"""Task 10.x tests: batch orchestrator, bounded concurrency, recovery.

Deterministic by design: the batch advances through synchronous ``step()``
rounds over injected fake workflows — no threads, no asyncio, no clock
dependence. The fake workflows record their active rounds and exact
semantic call counts, so the tests assert the concurrency bound and the
retry/idempotency semantics directly.
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.generation.batch import (
    BatchOrchestratorConfig,
    BatchRequest,
    BatchScriptGenerationOrchestrator,
    BatchState,
    ContentFailure,
    IdempotencyRegistry,
    TransportError,
    request_fingerprint,
)
from backend.application.script_authoring.generation.scheduler import BoundedScheduler


class _FakeWorkflow:
    """Scripted finite workflow: N semantic steps then terminal success."""

    def __init__(self, product_id: str, steps: int = 1) -> None:
        self.product_id = product_id
        self._remaining = steps
        self.semantic_calls = 0
        self.current_segment_index = 0
        self.plan_segment_count = steps
        self._active_rounds: list[int] = []
        self._transport_raises = 0
        self._content_fail = False
        self._cancelled = False
        self._last_snapshot: dict = {}

    def fail_content_on_next(self) -> None:
        self._content_fail = True

    def raise_transport_times(self, times: int) -> None:
        self._transport_raises = times

    def step(self) -> bool:
        if self._cancelled:
            return False
        if self._transport_raises > 0:
            self._transport_raises -= 1
            raise TransportError("provider unavailable")
        if self._content_fail:
            self._content_fail = False
            raise ContentFailure("segment failed gate")
        self.semantic_calls += 1
        self.current_segment_index += 1
        self._remaining -= 1
        return self._remaining > 0

    def is_terminal(self) -> bool:
        return self._remaining <= 0

    def snapshot(self) -> dict:
        self._last_snapshot = {
            "product_id": self.product_id,
            "current_segment_index": self.current_segment_index,
            "plan_segment_count": self.plan_segment_count,
            "semantic_calls": self.semantic_calls,
        }
        return self._last_snapshot

    def restore(self, state: dict) -> None:
        self.current_segment_index = int(state.get("current_segment_index", 0))
        self.plan_segment_count = int(state.get("plan_segment_count", 0))
        self._remaining = max(0, self.plan_segment_count - self.current_segment_index)
        # semantic_calls is intentionally NOT restored: it counts calls made
        # since this process started, so a recovered workflow reports only
        # calls it actually makes post-restart.
        self.semantic_calls = 0


class _FakeWorkflowFactory:
    """Makes distinct ``_FakeWorkflow`` per product, keeping a registry."""

    def __init__(self, steps: int = 1) -> None:
        self._steps = steps
        self.workflows: dict[str, _FakeWorkflow] = {}

    def __call__(self, product_id: str, target_duration_s: float) -> _FakeWorkflow:
        workflow = _FakeWorkflow(product_id, steps=self._steps)
        self.workflows[product_id] = workflow
        return workflow


def _request(
    product_ids: list[str],
    *,
    client_key: str = "client-key",
    revision: int = 1,
    durations: dict[str, float] | None = None,
) -> BatchRequest:
    durations = durations or {pid: 600.0 for pid in product_ids}
    return BatchRequest(
        script_set_id="script_set:abc",
        script_set_revision=revision,
        requested_products=tuple(product_ids),
        target_durations=tuple((pid, durations[pid]) for pid in product_ids),
        max_product_concurrency=3,
        max_attempts=3,
        model_fingerprint="fp-v1",
        client_key=client_key,
    )


def _orchestrator(
    factory: _FakeWorkflowFactory,
    *,
    max_concurrency: int = 3,
    max_attempts: int = 3,
    persist_store: dict | None = None,
    registry_store: dict | None = None,
) -> BatchScriptGenerationOrchestrator:
    persisted: list[BatchState] = []

    def _persist(state: BatchState) -> None:
        persisted.append(state.model_copy(deep=True))
        if persist_store is not None:
            persist_store["batch"] = state.model_dump()

    orchestrator = BatchScriptGenerationOrchestrator(
        factory,
        config=BatchOrchestratorConfig(
            max_product_concurrency=max_concurrency,
            max_attempts=max_attempts,
        ),
        persist=_persist,
        idempotency=IdempotencyRegistry(store=registry_store),
    )
    orchestrator._persisted = persisted  # type: ignore[attr-defined]
    return orchestrator


def _run_until_final(orchestrator: BatchScriptGenerationOrchestrator) -> BatchState:
    state = orchestrator.state
    for _ in range(1000):  # deterministic guard: batches are finite
        if state is not None and state.status in (
            "completed",
            "partial_completed",
            "failed",
            "cancelled",
        ):
            break
        state = orchestrator.step()
    return state  # type: ignore[return-value]


# ── 10.1/10.2: one workflow per product, bounded concurrency ──────────────


def test_creates_one_workflow_per_product_no_giant_response() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory)
    state, created = orch.start(_request(["P001", "P002", "P003"]))

    assert created is True
    assert sorted(orch.workflows) == ["P001", "P002", "P003"]
    assert len(factory.workflows) == 3  # one finite workflow per product
    assert state.status == "queued"


def test_bounded_concurrency_20_products_max_3_active() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory, max_concurrency=3)
    product_ids = [f"P{i:03d}" for i in range(20)]
    orch.start(_request(product_ids))

    _run_until_final(orch)

    assert orch.state.status == "completed"
    assert orch.state.completed == 20
    assert orch.scheduler.max_active_overlap() <= 3
    # every product ran exactly its one semantic step exactly once
    for workflow in factory.workflows.values():
        assert workflow.semantic_calls == 1


def test_segments_remain_sequential_inside_one_product() -> None:
    factory = _FakeWorkflowFactory(steps=5)
    orch = _orchestrator(factory)
    orch.start(_request(["P001"]))

    _run_until_final(orch)

    workflow = factory.workflows["P001"]
    assert workflow.semantic_calls == 5
    assert workflow.current_segment_index == 5
    assert orch.state.completed == 1
    assert orch.state.actual_semantic_calls == 5


# ── 10.4: sibling isolation / partial completion ──────────────────────────


def test_one_product_failure_preserves_completed_siblings() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory)
    orch.start(_request(["P001", "P002", "P003"]))

    factory.workflows["P002"].fail_content_on_next()
    final = _run_until_final(orch)

    assert final.status == "partial_completed"
    assert final.completed == 2
    assert final.failed == 1
    assert final.products["P001"].status == "completed"
    assert final.products["P002"].status == "failed"
    assert final.products["P003"].status == "completed"
    # failed product made zero content retries: one semantic attempt only
    assert factory.workflows["P002"].semantic_calls == 0


# ── 10.5: transport retry bound, attempt vs semantic count ────────────────


def test_transport_retries_do_not_inflate_semantic_count() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory, max_attempts=3)
    orch.start(_request(["P001"]))
    factory.workflows["P001"].raise_transport_times(2)

    _run_until_final(orch)

    state = orch.state
    assert state.status == "completed"
    assert state.products["P001"].transport_attempts == 2
    assert state.products["P001"].semantic_calls == 1  # attempt != semantic job
    assert state.actual_semantic_calls == 1
    assert factory.workflows["P001"].semantic_calls == 1


def test_transport_attempts_cap_exhausted_fails_product() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory, max_attempts=3)
    orch.start(_request(["P001"]))
    factory.workflows["P001"].raise_transport_times(5)  # exceeds cap

    final = _run_until_final(orch)

    assert final.status == "failed"
    assert final.products["P001"].status == "failed"
    assert final.products["P001"].transport_attempts == 3
    assert "max_attempts" in final.products["P001"].error
    assert factory.workflows["P001"].semantic_calls == 0


def test_content_failure_never_retries() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory, max_attempts=3)
    orch.start(_request(["P001"]))
    factory.workflows["P001"].fail_content_on_next()

    final = _run_until_final(orch)

    assert final.status == "failed"
    assert final.products["P001"].status == "failed"
    assert final.products["P001"].transport_attempts == 0
    assert factory.workflows["P001"].semantic_calls == 0  # no auto-regenerate


# ── 10.6: idempotency (double-click) ──────────────────────────────────────


def test_duplicate_request_returns_existing_workflow() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory)
    req = _request(["P001", "P002"])
    state1, created1 = orch.start(req)
    state2, created2 = orch.start(req)

    assert created1 is True
    assert created2 is False
    assert state1.batch_id == state2.batch_id
    assert len(factory.workflows) == 2  # no duplicate workflows created

    _run_until_final(orch)
    total_semantic = sum(w.semantic_calls for w in factory.workflows.values())
    assert total_semantic == 2  # double-click did not double-spend


def test_idempotency_survives_restart_via_persisted_registry() -> None:
    registry_store: dict = {}
    factory1 = _FakeWorkflowFactory()
    orch1 = _orchestrator(factory1, registry_store=registry_store)
    req = _request(["P001"])
    orch1.start(req)

    factory2 = _FakeWorkflowFactory()
    orch2 = _orchestrator(factory2, registry_store=registry_store)
    state, created = orch2.start(req)

    assert created is False
    assert state.batch_id == orch1.state.batch_id
    assert len(factory2.workflows) == 0  # restart did not re-create work


def test_request_fingerprint_is_stable_and_sensitive() -> None:
    req = _request(["P001", "P002"], client_key="k1")
    same = _request(["P001", "P002"], client_key="k1")
    different_key = _request(["P001", "P002"], client_key="k2")
    different_product = _request(["P001", "P003"], client_key="k1")

    assert request_fingerprint(req) == request_fingerprint(same)
    assert request_fingerprint(req) != request_fingerprint(different_key)
    assert request_fingerprint(req) != request_fingerprint(different_product)


# ── 10.7: cancellation ────────────────────────────────────────────────────


def test_cancel_stops_scheduling_and_preserves_completed() -> None:
    factory = _FakeWorkflowFactory(steps=5)
    orch = _orchestrator(factory)
    orch.start(_request(["P001", "P002", "P003"]))
    orch.step()  # round 1: promote P001..P003 (max 3), one segment each

    state = orch.cancel()

    assert state.status == "cancelled"
    assert state.cancelled == 3
    assert state.completed == 0
    # exactly one segment ran per product before cancellation
    for workflow in factory.workflows.values():
        assert workflow.semantic_calls == 1

    # stepping a cancelled batch is a no-op: no new semantic calls
    before = {pid: w.semantic_calls for pid, w in factory.workflows.items()}
    orch.step()
    assert {pid: w.semantic_calls for pid, w in factory.workflows.items()} == before


def test_cancel_preserves_completed_artifacts() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory)
    orch.start(_request(["P001", "P002"]))
    _run_until_final(orch)  # both complete
    before = orch.state.completed

    state = orch.cancel()  # completed batches are terminal: no-op

    assert before == 2
    assert state.status == "completed"  # cancel does not un-complete
    assert state.completed == 2


def test_cancel_partial_keeps_completed_and_cancels_rest() -> None:
    factory = _FakeWorkflowFactory(steps=2)
    orch = _orchestrator(factory, max_concurrency=1)
    orch.start(_request(["P001", "P002", "P003"]))

    orch.step()  # P001 runs segment 0
    orch.step()  # P001 runs segment 1 -> completes; P002 promoted
    state = orch.cancel()  # P002 active, P003 still queued

    assert state.status == "cancelled"
    assert state.completed == 1
    assert state.products["P001"].status == "completed"
    assert state.products["P001"].semantic_calls == 2  # artifact preserved
    assert state.products["P002"].status == "cancelled"
    assert state.products["P003"].status == "cancelled"


# ── 10.8: restart recovery ────────────────────────────────────────────────


def test_recover_batch_resumes_from_finite_state() -> None:
    persist_store: dict = {}
    factory1 = _FakeWorkflowFactory(steps=4)
    orch1 = _orchestrator(factory1, persist_store=persist_store)
    orch1.start(_request(["P001"]))
    orch1.step()  # segment 0 done (of 4)

    # process restart: a fresh orchestrator + fresh workflows
    factory2 = _FakeWorkflowFactory(steps=4)
    orch2 = _orchestrator(factory2)
    orch2.restore(BatchState.model_validate(persist_store["batch"]))

    final = _run_until_final(orch2)

    assert final.status == "completed"
    assert final.completed == 1
    workflow2 = factory2.workflows["P001"]
    # resumes at segment 1 — completed segment 0 was NOT re-run
    assert workflow2.semantic_calls == 3
    assert workflow2.current_segment_index == 4


def test_recover_batch_skips_completed_products() -> None:
    persist_store: dict = {}
    factory1 = _FakeWorkflowFactory()
    orch1 = _orchestrator(factory1, persist_store=persist_store)
    orch1.start(_request(["P001", "P002"]))
    _run_until_final(orch1)

    factory2 = _FakeWorkflowFactory()
    orch2 = _orchestrator(factory2)
    orch2.restore(BatchState.model_validate(persist_store["batch"]))

    assert orch2.state.status == "completed"
    # neither product re-ran after recovery
    for workflow in factory2.workflows.values():
        assert workflow.semantic_calls == 0
    assert orch2.scheduler.is_busy is False


def test_recover_batch_never_reinterprets_model_prose() -> None:
    """Recovery uses only persisted finite counters, not workflow output."""
    persist_store: dict = {}
    factory1 = _FakeWorkflowFactory(steps=2)
    orch1 = _orchestrator(factory1, persist_store=persist_store)
    orch1.start(_request(["P001"]))
    orch1.step()
    orch1.step()  # P001 done; snapshot has counters only
    saved = persist_store["batch"]["products"]["P001"]
    assert set(saved.keys()) >= {"current_segment_index", "plan_segment_count"}
    assert "display_text" not in saved  # no model prose in persisted state


# ── scheduler unit semantics ──────────────────────────────────────────────


def test_scheduler_rounds_and_windows() -> None:
    factory = _FakeWorkflowFactory()
    orch = _orchestrator(factory, max_concurrency=2)
    orch.start(_request(["A", "B", "C", "D"]))

    promoted_r1 = orch.scheduler.promote()
    assert [w.product_id for w in promoted_r1] == ["A", "B"]
    assert len(orch.scheduler.active()) == 2

    orch.scheduler.release(factory.workflows["A"])
    promoted_r2 = orch.scheduler.promote()
    assert [w.product_id for w in promoted_r2] == ["C"]
    assert [w.product_id for w in orch.scheduler.active()] == ["B", "C"]
    assert orch.scheduler.max_active_overlap() == 2


def test_scheduler_rejects_zero_concurrency() -> None:
    with pytest.raises(ValueError):
        BoundedScheduler(max_concurrency=0)
    with pytest.raises(ValueError):
        BatchOrchestratorConfig(max_product_concurrency=0)
