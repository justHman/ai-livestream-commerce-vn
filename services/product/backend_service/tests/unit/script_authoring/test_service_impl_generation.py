"""ScriptAuthoringServiceImpl AI generation unit tests (Change B, B6).

Covers the AI long-form methods against in-memory fake repositories and a
controllable fake EngineManager / gate — no Postgres, no real LLM.

RED before ``service_impl.py`` wires the AI methods: the four AI commands
raise ``llm_unavailable`` and the batch/SSE methods raise ``llm_unavailable``
instead of returning ``not_found`` / ``None``. GREEN once the B6 methods are
real.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from backend.application.script_authoring.gate.results import (
    GateRunResult,
    RuleSetFingerprint,
    RuleViolation,
    Severity,
)
from backend.application.script_authoring.models import (
    Approval,
    GateRun,
    GenerationBatch,
    GenerationJob,
    ScriptItem,
    ScriptSegment,
    ScriptSet,
    ScriptSource,
    ScriptState,
    ScriptVersion,
)
from backend.application.script_authoring.generation.batch import BatchState
from backend.application.script_authoring.repositories import StaleRevisionError
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig

PLAN_RESPONSE = "1. Mở đầu|Giới thiệu sản phẩm|600\n2. Nội dung|Lợi ích chính|600\n"
SEGMENT_RESPONSE = "Đây là nội dung quảng cáo cho sản phẩm này."


# ── fake gate ───────────────────────────────────────────────────────────────


class _FakeGate:
    """Duck-typed ``ScriptGate`` with a controllable outcome."""

    def __init__(self, result: GateRunResult) -> None:
        self._result = result
        self.full_calls = 0
        self.segment_calls = 0

    def run_full_script(self, segments, context) -> GateRunResult:
        self.full_calls += 1
        return self._result

    def run_segment(self, text, context) -> GateRunResult:
        self.segment_calls += 1
        return self._result


def _pass_result() -> GateRunResult:
    return GateRunResult(scope="full_script", fingerprint=RuleSetFingerprint())


def _fail_result() -> GateRunResult:
    return GateRunResult(
        scope="segment",
        fingerprint=RuleSetFingerprint(),
        violations=(
            RuleViolation(rule_id="STYLE_001", severity=Severity.ERROR, message="bad content"),
        ),
    )


# ── fake EngineManager ──────────────────────────────────────────────────────


class _FakeEngineManager:
    """Duck-typed ``EngineManager`` with a controllable sync LLM fn."""

    def __init__(self, llm_fn=None, cfg=None, failed: bool = False) -> None:
        self._llm_fn = llm_fn
        self._llm_cfg = cfg if cfg is not None else {"engine": "echo", "model": "fake"}
        self._llm_failed = failed
        self.llm = object() if llm_fn is not None else None

    @property
    def llm_cfg(self) -> dict:
        return self._llm_cfg

    @property
    def llm_failed(self) -> bool:
        return self._llm_failed

    def get_llm_fn(self):
        return self._llm_fn


def _echo_llm(prompt: str) -> str:
    if "PLAN" in prompt.upper():
        return PLAN_RESPONSE
    return SEGMENT_RESPONSE


# ── fake repositories ───────────────────────────────────────────────────────


class _FakeSetRepo:
    def __init__(self) -> None:
        self.rows: dict[str, ScriptSet] = {}

    async def get(self, set_id: str, *, conn=None) -> ScriptSet | None:
        return self.rows.get(set_id)

    async def insert(self, set_: ScriptSet, *, conn=None) -> None:
        self.rows[set_.id] = set_


class _FakeItemRepo:
    def __init__(self) -> None:
        self.rows: dict[str, ScriptItem] = {}

    async def get(self, item_id: str, *, conn=None) -> ScriptItem | None:
        return self.rows.get(item_id)

    async def get_by_product(self, set_id: str, product_id: str, *, conn=None) -> ScriptItem | None:
        for item in self.rows.values():
            if item.script_set_id == set_id and item.product_id == product_id:
                return item
        return None

    async def insert(self, item: ScriptItem, *, conn=None) -> None:
        self.rows[item.id] = item

    async def update(self, item: ScriptItem, *, expected_revision: int, conn=None) -> None:
        current = self.rows.get(item.id)
        if current is None or expected_revision != current.revision:
            raise StaleRevisionError(
                f"script_item {item.id}: revision {expected_revision} not current"
            )
        item.revision = current.revision + 1
        self.rows[item.id] = item


class _FakeVersionRepo:
    def __init__(self) -> None:
        self.rows: dict[str, ScriptVersion] = {}

    async def get(self, version_id: str, *, conn=None) -> ScriptVersion | None:
        version = self.rows.get(version_id)
        return version.model_copy(deep=True) if version is not None else None

    async def insert(self, version: ScriptVersion, *, conn=None) -> None:
        self.rows[version.id] = version.model_copy(deep=True)


class _FakeGateRunRepo:
    def __init__(self) -> None:
        self.rows: dict[str, GateRun] = {}
        self._by_item: dict[str, list[GateRun]] = {}

    async def insert(self, run: GateRun, *, conn=None) -> None:
        self.rows[run.id] = run
        self._by_item.setdefault(run.script_item_id, []).append(run)

    async def list_by_item(self, item_id: str, *, conn=None) -> list[GateRun]:
        return list(self._by_item.get(item_id, []))


class _FakeApprovalRepo:
    def __init__(self) -> None:
        self.rows: dict[str, Approval] = {}

    async def insert(self, approval: Approval, *, dependencies: dict, conn=None) -> None:
        self.rows[approval.id] = approval


class _FakePlanRepo:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def insert(self, plan, *, conn=None) -> None:
        self.rows[plan.id] = plan

    async def get_latest(self, item_id: str, *, conn=None):
        for plan in self.rows.values():
            if plan.script_item_id == item_id:
                return plan
        return None


class _FakeSegmentRepo:
    def __init__(self) -> None:
        self.rows: dict[str, ScriptSegment] = {}

    async def insert(self, segment: ScriptSegment, *, conn=None) -> None:
        self.rows[segment.id] = segment

    async def get(self, segment_id: str, *, conn=None) -> ScriptSegment | None:
        return self.rows.get(segment_id)

    async def list_by_plan(self, plan_id: str, *, conn=None) -> list[ScriptSegment]:
        return [s for s in self.rows.values() if s.plan_id == plan_id]


class _FakeBatchRepo:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[GenerationBatch, BatchState]] = {}

    async def insert(self, batch: GenerationBatch, *, state: BatchState, conn=None) -> None:
        self.rows[batch.id] = (batch, state.model_copy(deep=True))

    async def get(self, batch_id: str, *, conn=None) -> tuple[GenerationBatch, BatchState] | None:
        row = self.rows.get(batch_id)
        if row is None:
            return None
        return row[0], row[1].model_copy(deep=True)

    async def update_state(
        self, batch_id: str, *, state: BatchState, expected_revision: int, conn=None
    ) -> None:
        row = self.rows.get(batch_id)
        if row is None:
            raise StaleRevisionError(f"batch {batch_id}: not found")
        _, current_state = row
        if expected_revision != current_state.revision:
            raise StaleRevisionError(f"batch {batch_id}: revision {expected_revision} not current")
        self.rows[batch_id] = (row[0], state.model_copy(deep=True))

    async def find_by_idempotency(
        self, set_id: str, key: str, *, conn=None
    ) -> GenerationBatch | None:
        if not key:
            return None
        for batch, _state in self.rows.values():
            if batch.script_set_id == set_id and batch.idempotency_key == key:
                return batch
        return None


class _FakeJobRepo:
    def __init__(self) -> None:
        self.rows: dict[str, GenerationJob] = {}

    async def insert(self, job: GenerationJob, *, conn=None) -> None:
        self.rows[job.id] = job

    async def get(self, job_id: str, *, conn=None) -> GenerationJob | None:
        return self.rows.get(job_id)

    async def find_by_idempotency(
        self, item_id: str, intent: str, key: str, *, conn=None
    ) -> GenerationJob | None:
        if not key:
            return None
        for job in self.rows.values():
            if (
                job.script_item_id == item_id
                and job.intent.value == intent
                and job.idempotency_key == key
            ):
                return job
        return None

    async def update(self, job: GenerationJob, *, expected_revision: int = 0, conn=None) -> None:
        self.rows[job.id] = job


class _FakeIdempotencyRepo:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, fingerprint: str, *, conn=None) -> str | None:
        return self.rows.get(fingerprint)

    async def register(self, fingerprint: str, batch_id: str, *, conn=None) -> None:
        self.rows.setdefault(fingerprint, batch_id)


class _FakeRepos:
    """Repository surface the service talks to (zero PG)."""

    def __init__(self) -> None:
        self.script_sets = _FakeSetRepo()
        self.items = _FakeItemRepo()
        self.versions = _FakeVersionRepo()
        self.gate_runs = _FakeGateRunRepo()
        self.approvals = _FakeApprovalRepo()
        self.plans = _FakePlanRepo()
        self.segments = _FakeSegmentRepo()
        self.batches = _FakeBatchRepo()
        self.jobs = _FakeJobRepo()
        self.idempotency = _FakeIdempotencyRepo()

    @asynccontextmanager
    async def transaction(self):
        yield object()


def _make_service(
    gate: _FakeGate,
    engine_manager: _FakeEngineManager | None = None,
    repos: _FakeRepos | None = None,
) -> tuple[ScriptAuthoringServiceImpl, _FakeRepos]:
    repos = repos or _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos,
        config=ScriptAuthoringConfig(),
        gate=gate,
        engine_manager=engine_manager,
    )
    return service, repos


async def _new_set(repos: _FakeRepos, service: ScriptAuthoringServiceImpl, product_ids=None):
    return await service.create_script_set(
        name="Set demo",
        transition_policy="ORDER_AGNOSTIC",
        product_ids=product_ids or ["P1"],
        brief=None,
    )


async def _wait_for_state(
    repos: _FakeRepos, set_id: str, product_id: str, state: ScriptState, tries: int = 500
) -> ScriptItem:
    item = await repos.items.get_by_product(set_id, product_id)
    for _ in range(tries):
        item = await repos.items.get_by_product(set_id, product_id)
        if item.state is state:
            return item
        await asyncio.sleep(0.01)
    return item


# ── llm_unavailable (Decision 1) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_commands_raise_llm_unavailable_when_engine_manager_none() -> None:
    service, repos = _make_service(_FakeGate(_fail_result()), engine_manager=None)
    set_id = (await _new_set(repos, service))["id"]
    # fix needs a gate-failed version to reach the llm check; start_generation /
    # regenerate / start_batch check llm availability before doing any work.
    await service.save_draft(
        set_id=set_id, product_id="P1", display_text="x", spoken_text="x", revision=None
    )
    await service.submit_for_gate(set_id=set_id, product_id="P1")
    calls = [
        (
            "start_generation",
            dict(
                set_id=set_id,
                product_id="P1",
                target_duration_s=600,
                intent="selling",
                idempotency_key="k",
            ),
        ),
        ("fix_with_ai", dict(set_id=set_id, product_id="P1", idempotency_key="k")),
        (
            "regenerate_segment",
            dict(set_id=set_id, product_id="P1", segment_index=0, idempotency_key="k"),
        ),
        (
            "start_batch_generation",
            dict(set_id=set_id, product_ids=["P1"], target_duration_s=600, idempotency_key="k"),
        ),
    ]
    for name, kwargs in calls:
        with pytest.raises(ScriptAuthoringError) as exc:
            await getattr(service, name)(**kwargs)
        assert exc.value.code == "llm_unavailable", name


@pytest.mark.asyncio
async def test_start_generation_llm_unavailable_when_engine_none() -> None:
    em = _FakeEngineManager(llm_fn=_echo_llm, cfg={"engine": "none"})
    service, repos = _make_service(_FakeGate(_pass_result()), engine_manager=em)
    set_id = (await _new_set(repos, service))["id"]
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="k",
        )
    assert exc.value.code == "llm_unavailable"


@pytest.mark.asyncio
async def test_start_generation_llm_unavailable_when_llm_failed() -> None:
    em = _FakeEngineManager(llm_fn=_echo_llm, cfg={"engine": "vllm"}, failed=True)
    service, repos = _make_service(_FakeGate(_pass_result()), engine_manager=em)
    set_id = (await _new_set(repos, service))["id"]
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="k",
        )
    assert exc.value.code == "llm_unavailable"


@pytest.mark.asyncio
async def test_start_generation_llm_unavailable_when_get_llm_fn_none() -> None:
    em = _FakeEngineManager(llm_fn=None, cfg={"engine": "vllm"})
    service, repos = _make_service(_FakeGate(_pass_result()), engine_manager=em)
    set_id = (await _new_set(repos, service))["id"]
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="k",
        )
    assert exc.value.code == "llm_unavailable"


@pytest.mark.asyncio
async def test_start_generation_llm_unavailable_when_engine_empty_or_none() -> None:
    for cfg in ({"engine": ""}, {"engine": None}):
        em = _FakeEngineManager(llm_fn=_echo_llm, cfg=cfg)
        service, repos = _make_service(_FakeGate(_pass_result()), engine_manager=em)
        set_id = (await _new_set(repos, service))["id"]
        with pytest.raises(ScriptAuthoringError) as exc:
            await service.start_generation(
                set_id=set_id,
                product_id="P1",
                target_duration_s=600,
                intent="selling",
                idempotency_key="k",
            )
        assert exc.value.code == "llm_unavailable"


# ── start_generation wire + background completion ───────────────────────────


@pytest.mark.asyncio
async def test_start_generation_wire_shape_and_completes_to_reviewable() -> None:
    service, repos = _make_service(
        _FakeGate(_pass_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    result = await service.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="gen-1",
    )
    assert result["workflow_id"].startswith("job:")
    assert result["product_id"] == "P1"
    assert result["status"] == "queued"
    assert "idempotent" not in result

    item = await _wait_for_state(repos, set_id, "P1", ScriptState.REVIEWABLE)
    assert item.state is ScriptState.REVIEWABLE
    assert item.current_version_id is not None
    version = repos.versions.rows[item.current_version_id]
    assert version.source is ScriptSource.AI_GENERATE
    assert len(repos.plans.rows) == 1
    assert len(repos.segments.rows) == 2  # K=2 placeholder-free real segments
    assert len(repos.versions.rows) == 1


@pytest.mark.asyncio
async def test_start_generation_idempotent_duplicate_returns_same_job() -> None:
    service, repos = _make_service(
        _FakeGate(_pass_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    first = await service.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="gen-dup",
    )
    second = await service.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="gen-dup",
    )
    assert second["workflow_id"] == first["workflow_id"]
    assert second.get("idempotent") is True


@pytest.mark.asyncio
async def test_start_generation_gate_fail_lands_gate_failed() -> None:
    service, repos = _make_service(
        _FakeGate(_fail_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    await service.start_generation(
        set_id=set_id, product_id="P1", target_duration_s=600, intent="selling", idempotency_key="k"
    )
    item = await _wait_for_state(repos, set_id, "P1", ScriptState.GATE_FAILED)
    assert item.state is ScriptState.GATE_FAILED


@pytest.mark.asyncio
async def test_start_generation_unknown_set_or_product_not_found() -> None:
    service, repos = _make_service(
        _FakeGate(_pass_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.start_generation(
            set_id="nope",
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="k",
        )
    assert exc.value.code == "not_found"
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.start_generation(
            set_id=set_id,
            product_id="P999",
            target_duration_s=600,
            intent="selling",
            idempotency_key="k",
        )
    assert exc.value.code == "not_found"


# ── regenerate_segment ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regenerate_segment_illegal_from_empty() -> None:
    service, repos = _make_service(
        _FakeGate(_pass_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.regenerate_segment(
            set_id=set_id, product_id="P1", segment_index=0, idempotency_key="k"
        )
    assert exc.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_regenerate_segment_wire_shape_from_gate_failed() -> None:
    service, repos = _make_service(
        _FakeGate(_fail_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    await service.save_draft(
        set_id=set_id, product_id="P1", display_text="x", spoken_text="x", revision=None
    )
    await service.submit_for_gate(set_id=set_id, product_id="P1")  # GATE_FAILED with fake gate
    result = await service.regenerate_segment(
        set_id=set_id, product_id="P1", segment_index=0, idempotency_key="reg-1"
    )
    assert result["workflow_id"].startswith("job:")
    assert result["product_id"] == "P1"
    assert result["segment_index"] == 0
    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_regenerate_and_fix_missing_set_or_product_not_found() -> None:
    service, repos = _make_service(
        _FakeGate(_pass_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    calls = [
        (
            "regenerate_segment",
            dict(set_id="nope", product_id="P1", segment_index=0, idempotency_key="k"),
        ),
        (
            "regenerate_segment",
            dict(set_id=set_id, product_id="P999", segment_index=0, idempotency_key="k"),
        ),
        ("fix_with_ai", dict(set_id="nope", product_id="P1", idempotency_key="k")),
        ("fix_with_ai", dict(set_id=set_id, product_id="P999", idempotency_key="k")),
    ]
    for name, kwargs in calls:
        with pytest.raises(ScriptAuthoringError) as exc:
            await getattr(service, name)(**kwargs)
        assert exc.value.code == "not_found", name


# ── fix_with_ai ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fix_with_ai_not_eligible_from_empty() -> None:
    service, repos = _make_service(
        _FakeGate(_pass_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.fix_with_ai(set_id=set_id, product_id="P1", idempotency_key="k")
    assert exc.value.code == "fix_not_eligible"


@pytest.mark.asyncio
async def test_fix_with_ai_happy_path_produces_draft_ai_fix_version() -> None:
    service, repos = _make_service(
        _FakeGate(_fail_result()), engine_manager=_FakeEngineManager(_echo_llm)
    )
    set_id = (await _new_set(repos, service))["id"]
    await service.save_draft(
        set_id=set_id, product_id="P1", display_text="x", spoken_text="x", revision=None
    )
    await service.submit_for_gate(set_id=set_id, product_id="P1")  # GATE_FAILED
    result = await service.fix_with_ai(set_id=set_id, product_id="P1", idempotency_key="fix-1")
    assert result["workflow_id"].startswith("job:")
    assert result["product_id"] == "P1"
    assert result["status"] == "queued"

    # Poll until the background fix lands in DRAFT (AI_FIX source).
    item = None
    for _ in range(500):
        item = await repos.items.get_by_product(set_id, "P1")
        if item.state is ScriptState.DRAFT and item.current_version_id is not None:
            version = repos.versions.rows.get(item.current_version_id)
            if version is not None and version.source is ScriptSource.AI_FIX:
                break
        await asyncio.sleep(0.01)
    assert item is not None and item.state is ScriptState.DRAFT
    version = repos.versions.rows[item.current_version_id]
    assert version.source is ScriptSource.AI_FIX


# ── batch / SSE without LLM (must NOT raise llm_unavailable) ────────────────


@pytest.mark.asyncio
async def test_get_batch_unknown_raises_not_found_not_llm_unavailable() -> None:
    service, repos = _make_service(_FakeGate(_pass_result()), engine_manager=None)
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.get_batch(set_id="s", batch_id="b1")
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_cancel_batch_unknown_raises_not_found_not_llm_unavailable() -> None:
    service, repos = _make_service(_FakeGate(_pass_result()), engine_manager=None)
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.cancel_batch(set_id="s", batch_id="b1")
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_get_batch_events_snapshot_unknown_returns_none() -> None:
    service, repos = _make_service(_FakeGate(_pass_result()), engine_manager=None)
    assert await service.get_batch_events_snapshot(set_id="s", batch_id="b1") is None


@pytest.mark.asyncio
async def test_stream_batch_events_unknown_does_not_raise_llm_unavailable() -> None:
    service, repos = _make_service(_FakeGate(_pass_result()), engine_manager=None)
    events = []
    async for event in service.stream_batch_events(set_id="s", batch_id="b1"):
        events.append(event)
        break  # never let an unknown-batch stream loop forever
    # Unknown batch: stream yields nothing (the router emits batch.error itself).
    assert events == []
