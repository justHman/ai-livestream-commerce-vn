"""ScriptAuthoringServiceImpl core unit tests (Change B, B4).

RED before ``application/script_authoring/service_impl.py`` exists: imports
fail. GREEN once the concrete zero-LLM service implements the core wire
shapes, exception mapping, ``update_script_set``, and approval flows over
in-memory fake repositories and an injectable gate.
"""

from __future__ import annotations

import queue
from contextlib import asynccontextmanager

import pytest

from backend.application.script_authoring.compile import compile_spoken_text
from backend.application.script_authoring.gate.results import (
    GateRunResult,
    RuleSetFingerprint,
    RuleViolation,
    Severity,
)
from backend.application.script_authoring.generation.batch import BatchState
from backend.application.script_authoring.models import (
    Approval,
    GateRun,
    ScriptItem,
    ScriptSet,
    ScriptState,
    ScriptVersion,
)
from backend.application.script_authoring.repositories import StaleRevisionError
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig


# ── fake gate (controllable deterministic outcome) ──────────────────────────


class _FakeGate:
    """Duck-typed ``ScriptGate`` returning a fixed result every run."""

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
        scope="full_script",
        fingerprint=RuleSetFingerprint(),
        violations=(
            RuleViolation(
                rule_id="VN_SPELLING_001",
                severity=Severity.ERROR,
                message="phát hiện lỗi chính tả",
            ),
        ),
    )


# ── fake repositories (in-memory, revision-guarded) ─────────────────────────


class _FakeSetRepo:
    def __init__(self) -> None:
        self.rows: dict[str, ScriptSet] = {}

    async def get(self, set_id: str, *, conn=None) -> ScriptSet | None:
        return self.rows.get(set_id)

    async def insert(self, set_: ScriptSet, *, conn=None) -> None:
        self.rows[set_.id] = set_

    async def update(self, set_: ScriptSet, *, expected_revision: int, conn=None) -> None:
        current = self.rows.get(set_.id)
        if current is None or expected_revision != current.revision:
            raise StaleRevisionError(
                f"script_set {set_.id}: revision {expected_revision} not current"
            )
        self.rows[set_.id] = set_


class _FakeItemRepo:
    def __init__(self) -> None:
        self.rows: dict[str, ScriptItem] = {}
        self.fail_update = False

    async def get(self, item_id: str, *, conn=None) -> ScriptItem | None:
        return self.rows.get(item_id)

    async def get_by_product(self, set_id: str, product_id: str, *, conn=None) -> ScriptItem | None:
        for item in self.rows.values():
            if item.script_set_id == set_id and item.product_id == product_id:
                return item
        return None

    async def list_by_set(self, set_id: str, *, conn=None) -> list[ScriptItem]:
        return [i for i in self.rows.values() if i.script_set_id == set_id]

    async def insert(self, item: ScriptItem, *, conn=None) -> None:
        self.rows[item.id] = item

    async def update(self, item: ScriptItem, *, expected_revision: int, conn=None) -> None:
        if self.fail_update:
            raise StaleRevisionError(
                f"script_item {item.id}: revision {expected_revision} not current"
            )
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
        # Fresh copy per read (mirrors the SQL repository's per-query
        # deserialization); the workflow may mutate its in-memory copy without
        # ever touching the immutable persisted row.
        version = self.rows.get(version_id)
        return version.model_copy(deep=True) if version is not None else None

    async def insert(self, version: ScriptVersion, *, conn=None) -> None:
        # Store a deep copy: version rows are immutable once persisted, so
        # later in-memory state mutations (e.g. workflow.submit()) never leak
        # into the stored row (mirrors the SQL INSERT-ONLY repository).
        self.rows[version.id] = version.model_copy(deep=True)


class _FakeGateRunRepo:
    def __init__(self) -> None:
        self.rows: dict[str, GateRun] = {}

    async def insert(self, run: GateRun, *, conn=None) -> None:
        self.rows[run.id] = run

    async def list_by_item(self, item_id: str, *, conn=None) -> list[GateRun]:
        return [r for r in self.rows.values() if r.script_item_id == item_id]

    async def latest_for_version(self, version_id: str, *, conn=None) -> GateRun | None:
        return None


class _FakeApprovalRepo:
    def __init__(self) -> None:
        self.rows: dict[str, Approval] = {}
        self.dependencies: dict[str, dict] = {}

    async def insert(self, approval: Approval, *, dependencies: dict, conn=None) -> None:
        self.rows[approval.id] = approval
        self.dependencies[approval.id] = dependencies


class _FakeBatchRepo:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def get(self, batch_id: str, *, conn=None):
        return None

    async def insert(self, batch, *, state, conn=None) -> None:
        self.rows[batch.id] = batch

    async def find_by_idempotency(self, set_id: str, key: str, *, conn=None):
        return None


class _FakeJobRepo:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def find_by_idempotency(self, item_id: str, intent: str, key: str, *, conn=None):
        return None

    async def insert(self, job, *, conn=None) -> None:
        self.rows[job.id] = job


class _FakeIdempotencyRepo:
    async def get(self, fingerprint: str, *, conn=None):
        return None

    async def register(self, fingerprint: str, batch_id: str, *, conn=None) -> None:
        pass


class _FakePlanRepo:
    async def insert(self, plan, *, conn=None) -> None:
        pass


class _FakeSegmentRepo:
    async def insert(self, segment, *, conn=None) -> None:
        pass


class _FakeRepos:
    """Minimal repository surface the service talks to (zero PG)."""

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


def _make_service(gate: _FakeGate) -> ScriptAuthoringServiceImpl:
    return ScriptAuthoringServiceImpl(_FakeRepos(), config=ScriptAuthoringConfig(), gate=gate)


def _empty_item_wire(state: str = "EMPTY") -> dict:
    """Enriched per-item read wire for an item with no version yet (HIGH-2)."""
    return {
        "state": state,
        "current_version_id": None,
        "approved_version_id": None,
        "current_version": None,
        "gate": None,
    }


# ── ScriptSet aggregate (task 11.2) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_script_set_wire_shape_and_shop_id() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    result = await service.create_script_set(
        name="Set demo",
        transition_policy="ORDER_AGNOSTIC",
        product_ids=["P001", "P002"],
        brief={"title": "T", "shop_name": "Shop A", "host_name": "Host", "note": "n"},
    )
    assert result["id"].startswith("script_set:")
    assert result["name"] == "Set demo"
    assert result["transition_policy"] == "ORDER_AGNOSTIC"
    assert result["product_ids"] == ["P001", "P002"]
    assert result["revision"] == 0
    assert result["items"] == {"P001": _empty_item_wire(), "P002": _empty_item_wire()}
    assert "brief" not in result
    stored = repos.script_sets.rows[result["id"]]
    assert stored.shop_id == "Shop A"


@pytest.mark.asyncio
async def test_create_script_set_default_shop_id_when_no_brief_shop() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    result = await service.create_script_set(
        name="Set", transition_policy="ORDER_AWARE", product_ids=["P1"], brief=None
    )
    assert result["transition_policy"] == "ORDER_AWARE"
    assert result["items"] == {"P1": _empty_item_wire()}


@pytest.mark.asyncio
async def test_get_script_set_wire_shape_no_brief_and_missing() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="Set", transition_policy="ORDER_AWARE", product_ids=["P1"], brief=None
    )
    result = await service.get_script_set(set_id=created["id"])
    assert result is not None
    assert result["transition_policy"] == "ORDER_AWARE"
    assert "brief" not in result
    assert result["items"] == {"P1": _empty_item_wire()}
    assert await service.get_script_set(set_id="nope") is None


@pytest.mark.asyncio
async def test_update_script_set_adds_items_and_bumps_revision() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    result = await service.update_script_set(
        set_id=created["id"],
        name="B",
        transition_policy="ORDER_AWARE",
        product_ids=["P1", "P2", "P1"],
        brief=None,
        revision=0,
    )
    assert result is not None
    assert result["name"] == "B"
    assert result["transition_policy"] == "ORDER_AWARE"
    assert result["revision"] == 1
    assert result["items"] == {"P1": _empty_item_wire(), "P2": _empty_item_wire()}


@pytest.mark.asyncio
async def test_update_script_set_stale_revision_409() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.update_script_set(
            set_id=created["id"],
            name="X",
            transition_policy=None,
            product_ids=None,
            brief=None,
            revision=5,
        )
    assert exc.value.code == "stale_revision"


@pytest.mark.asyncio
async def test_update_script_set_brief_preserves_policy() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    result = await service.update_script_set(
        set_id=created["id"],
        name=None,
        transition_policy=None,
        product_ids=None,
        brief={"title": "New title", "shop_name": "Shop B"},
        revision=0,
    )
    assert result is not None
    assert result["name"] == "A"
    assert result["transition_policy"] == "ORDER_AGNOSTIC"
    assert result["revision"] == 1


@pytest.mark.asyncio
async def test_update_script_set_unknown_returns_none() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    assert (
        await service.update_script_set(
            set_id="nope",
            name="x",
            transition_policy=None,
            product_ids=None,
            brief=None,
            revision=None,
        )
        is None
    )


# ── draft / submit / gate (task 11.3) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_save_draft_compiles_spoken_text_and_persists_version() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    result = await service.save_draft(
        set_id=created["id"],
        product_id="P1",
        display_text="Kem ABC giá 299.000 đồng.",
        spoken_text=None,
        revision=None,
    )
    assert result == {"ok": True, "product_id": "P1", "state": "DRAFT"}
    item = await repos.items.get_by_product(created["id"], "P1")
    assert item.state is ScriptState.DRAFT
    assert item.current_version_id is not None
    version = repos.versions.rows[item.current_version_id]
    assert version.spoken_text == compile_spoken_text("Kem ABC giá 299.000 đồng.").spoken_text


@pytest.mark.asyncio
async def test_save_draft_uses_provided_spoken_text() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    await service.save_draft(
        set_id=created["id"],
        product_id="P1",
        display_text="Kem tốt",
        spoken_text="Kem tot doc la la",
        revision=None,
    )
    item = await repos.items.get_by_product(created["id"], "P1")
    version = repos.versions.rows[item.current_version_id]
    assert version.spoken_text == "Kem tot doc la la"


@pytest.mark.asyncio
async def test_save_draft_illegal_transition_mapping() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    item = await repos.items.get_by_product(created["id"], "P1")
    item.state = ScriptState.CANCELLED
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.save_draft(
            set_id=created["id"],
            product_id="P1",
            display_text="x",
            spoken_text="y",
            revision=None,
        )
    assert exc.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_save_draft_stale_revision_mapping() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    repos.items.fail_update = True
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.save_draft(
            set_id=created["id"],
            product_id="P1",
            display_text="x",
            spoken_text="y",
            revision=None,
        )
    assert exc.value.code == "stale_revision"


@pytest.mark.asyncio
async def test_submit_gate_pass_becomes_reviewable() -> None:
    gate = _FakeGate(_pass_result())
    service = _make_service(gate)
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    await service.save_draft(
        set_id=created["id"],
        product_id="P1",
        display_text="Kem tốt",
        spoken_text="Kem tốt",
        revision=None,
    )
    result = await service.submit_for_gate(set_id=created["id"], product_id="P1")
    assert result["ok"] is True
    assert result["state"] == "REVIEWABLE"
    assert result["gate"] == {"state": "passed", "violations": []}
    assert gate.full_calls == 1


@pytest.mark.asyncio
async def test_submit_gate_fail_is_domain_state() -> None:
    service = _make_service(_FakeGate(_fail_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    await service.save_draft(
        set_id=created["id"],
        product_id="P1",
        display_text="rao bán không đúng sự thật",
        spoken_text="rao bán không đúng sự thật",
        revision=None,
    )
    result = await service.submit_for_gate(set_id=created["id"], product_id="P1")
    assert result["state"] == "GATE_FAILED"
    assert result["gate"]["state"] == "gate_failed"
    assert result["gate"]["violations"][0]["rule_id"] == "VN_SPELLING_001"


@pytest.mark.asyncio
async def test_submit_without_draft_illegal_transition() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.submit_for_gate(set_id=created["id"], product_id="P1")
    assert exc.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_submit_does_not_update_immutable_version_row() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    await service.save_draft(
        set_id=created["id"],
        product_id="P1",
        display_text="Kem tốt",
        spoken_text="Kem tốt",
        revision=None,
    )
    item = await repos.items.get_by_product(created["id"], "P1")
    version_id = item.current_version_id
    await service.submit_for_gate(set_id=created["id"], product_id="P1")
    assert repos.versions.rows[version_id].state is ScriptState.DRAFT


# ── generation preview (task 11.4) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_product_wire_shape() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    result = await service.preview_product(
        set_id=created["id"], product_id="P1", target_duration_s=600
    )
    assert result["product_id"] == "P1"
    assert result["target_duration_s"] == 600
    assert result["planned_segment_count"] == 2
    assert result["estimated_semantic_calls"] == 1 + result["planned_segment_count"]


@pytest.mark.asyncio
async def test_preview_out_of_range_illegal_transition() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.preview_product(set_id=created["id"], product_id="P1", target_duration_s=60)
    assert exc.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_preview_unknown_product_404() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.preview_product(
            set_id=created["id"], product_id="P999", target_duration_s=600
        )
    assert exc.value.code == "not_found"


# ── approval (task 11.7) ────────────────────────────────────────────────────


async def _reviewable(service, repos, set_id: str, product_id: str) -> str:
    await service.save_draft(
        set_id=set_id,
        product_id=product_id,
        display_text="Kem tốt",
        spoken_text="Kem tốt",
        revision=None,
    )
    await service.submit_for_gate(set_id=set_id, product_id=product_id)
    item = await repos.items.get_by_product(set_id, product_id)
    return item.current_version_id


@pytest.mark.asyncio
async def test_approve_product_persists_approval_and_gate_run() -> None:
    repos = _FakeRepos()
    gate = _FakeGate(_pass_result())
    service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig(), gate=gate)
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    version_id = await _reviewable(service, repos, created["id"], "P1")
    result = await service.approve_product(
        set_id=created["id"], product_id="P1", version_id=version_id, actor="nam"
    )
    assert result["ok"] is True
    assert result["state"] == "APPROVED"
    assert result["approval"]["version_id"] == version_id
    assert result["approval"]["actor"] == "nam"
    assert result["approval"]["approved_at"]
    item = await repos.items.get_by_product(created["id"], "P1")
    assert item.state is ScriptState.APPROVED
    assert item.approved_version_id == version_id
    assert len(repos.approvals.rows) == 1
    assert len(repos.gate_runs.rows) == 2  # one from submit + one from approve
    assert gate.full_calls == 2  # gate ran exactly once during approve


@pytest.mark.asyncio
async def test_approve_wrong_version_illegal_transition() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.approve_product(
            set_id=created["id"], product_id="P1", version_id="wrong-version", actor="nam"
        )
    assert exc.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_approve_not_reviewable_illegal_transition() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    await service.save_draft(
        set_id=created["id"],
        product_id="P1",
        display_text="Kem tốt",
        spoken_text="Kem tốt",
        revision=None,
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.approve_product(
            set_id=created["id"], product_id="P1", version_id="v1", actor="nam"
        )
    assert exc.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_approve_batch_wire() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1", "P2"], brief=None
    )
    version_ids: dict[str, str] = {}
    for pid in ("P1", "P2"):
        version_ids[pid] = await _reviewable(service, repos, created["id"], pid)
    result = await service.approve_batch(
        set_id=created["id"], product_ids=["P1", "P2"], version_ids=version_ids, actor="nam"
    )
    assert result["ok"] is True
    assert result["approvals"]["P1"]["state"] == "APPROVED"
    assert result["approvals"]["P2"]["approval"]["version_id"] == version_ids["P2"]
    assert len(repos.approvals.rows) == 2


# ── AI commands without an engine manager (B6) ──────────────────────────────


@pytest.mark.asyncio
async def test_ai_commands_raise_llm_unavailable_without_engine_manager() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_fail_result())
    )
    set_id = (
        await service.create_script_set(
            name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
        )
    )["id"]
    # fix / regenerate need a gate-failed version to reach the llm check.
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
async def test_batch_and_sse_methods_do_not_require_llm() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    # Unknown batch: the batch/SSE methods report not_found / None instead of
    # llm_unavailable — LLM availability is orthogonal to batch reads.
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.get_batch(set_id="s", batch_id="b1")
    assert exc.value.code == "not_found"
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.cancel_batch(set_id="s", batch_id="b1")
    assert exc.value.code == "not_found"
    assert await service.get_batch_events_snapshot(set_id="s", batch_id="b1") is None
    events: list[dict[str, str]] = []
    async for event in service.stream_batch_events(set_id="s", batch_id="b1"):
        events.append(event)
        break
    assert events == []


# ── C10 coverage additions: error branches (in-memory, no PG needed) ─────────


class _FakeSetRepoRaisesOnUpdate(_FakeSetRepo):
    async def update(self, set_: ScriptSet, *, expected_revision: int, conn=None) -> None:
        raise StaleRevisionError(f"script_set {set_.id}: revision {expected_revision} not current")


class _StaleBatchRepo:
    """Batch repo whose snapshots always lose the optimistic-lock race."""

    async def get(self, batch_id: str, *, conn=None):
        state = BatchState(batch_id=batch_id, script_set_id="s")
        state.revision = 1
        return object(), state

    async def update_state(
        self,
        batch_id: str,
        *,
        state,
        expected_revision: int,
        lease_owner: str | None = None,
        lease_epoch: int | None = None,
        lease_duration_s: int = 300,
        conn=None,
    ):
        raise StaleRevisionError(f"batch {batch_id}: revision {expected_revision} not current")


@pytest.mark.asyncio
async def test_submit_current_draft_version_missing() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    item = await repos.items.get_by_product(created["id"], "P1")
    item.current_version_id = "script_version:missingmissingmissingmissingmissingmis"
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.submit_for_gate(set_id=created["id"], product_id="P1")
    assert exc.value.code == "illegal_transition"
    assert "current draft version is missing" in exc.value.message


@pytest.mark.asyncio
async def test_approve_product_version_not_found() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    item = await repos.items.get_by_product(created["id"], "P1")
    item.state = ScriptState.REVIEWABLE
    item.current_version_id = "script_version:missingmissingmissingmissingmissingmis"
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.approve_product(
            set_id=created["id"], product_id="P1", version_id=item.current_version_id, actor="nam"
        )
    assert exc.value.code == "not_found"
    assert "script version" in exc.value.message


@pytest.mark.asyncio
async def test_approve_batch_missing_version_id() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1", "P2"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.approve_batch(
            set_id=created["id"], product_ids=["P2"], version_ids={}, actor="nam"
        )
    assert exc.value.code == "illegal_transition"
    assert "missing version_id for P2" in exc.value.message


@pytest.mark.asyncio
async def test_update_script_set_name_only_preserves_policy() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    result = await service.update_script_set(
        set_id=created["id"],
        name="B",
        transition_policy=None,
        product_ids=None,
        brief=None,
        revision=None,
    )
    assert result is not None
    assert result["name"] == "B"
    assert result["transition_policy"] == "ORDER_AGNOSTIC"


@pytest.mark.asyncio
async def test_update_script_set_transition_only() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    result = await service.update_script_set(
        set_id=created["id"],
        name=None,
        transition_policy="ORDER_AWARE",
        product_ids=None,
        brief=None,
        revision=None,
    )
    assert result is not None
    assert result["name"] == "A"
    assert result["transition_policy"] == "ORDER_AWARE"


@pytest.mark.asyncio
async def test_update_script_set_repo_stale_revision_mapping() -> None:
    repos = _FakeRepos()
    repos.script_sets = _FakeSetRepoRaisesOnUpdate()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.update_script_set(
            set_id=created["id"],
            name="B",
            transition_policy=None,
            product_ids=None,
            brief=None,
            revision=None,
        )
    assert exc.value.code == "stale_revision"


@pytest.mark.asyncio
async def test_drain_batch_persists_skips_stale_snapshot() -> None:
    repos = _FakeRepos()
    repos.batches = _StaleBatchRepo()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    persist_queue: queue.Queue[BatchState] = queue.Queue()
    persist_queue.put(BatchState(batch_id="b1", script_set_id="s"))
    # A concurrent drain already applied the snapshot -> update_state raises
    # StaleRevisionError -> the drain must swallow it and continue.
    await service._drain_batch_persists("b1", persist_queue)  # noqa: SLF001


@pytest.mark.asyncio
async def test_safe_llm_returns_none_when_unavailable() -> None:
    service = ScriptAuthoringServiceImpl(
        _FakeRepos(), config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    assert service._safe_llm() is None  # noqa: SLF001


class _RecoveryBatchRepo:
    """Batch repo returning a recoverable state with no per-product snapshots."""

    async def get(self, batch_id: str, *, conn=None):
        state = BatchState(batch_id=batch_id, script_set_id="s", requested_products=[])
        return object(), state

    async def insert(self, batch, *, state, conn=None) -> None:
        pass

    async def find_by_idempotency(self, set_id: str, key: str, *, conn=None):
        return None


@pytest.mark.asyncio
async def test_save_draft_revision_mismatch_stale() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.save_draft(
            set_id=created["id"], product_id="P1", display_text="x", spoken_text="x", revision=5
        )
    assert exc.value.code == "stale_revision"


@pytest.mark.asyncio
async def test_submit_and_preview_missing_product_not_found() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.submit_for_gate(set_id=created["id"], product_id="P999")
    assert exc.value.code == "not_found"
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.preview_product(
            set_id=created["id"], product_id="P999", target_duration_s=600
        )
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_preview_and_approve_missing_set_not_found() -> None:
    service = _make_service(_FakeGate(_pass_result()))
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.preview_product(set_id="nope", product_id="P1", target_duration_s=600)
    assert exc.value.code == "not_found"
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.approve_product(set_id="nope", product_id="P1", version_id="v1", actor="nam")
    assert exc.value.code == "not_found"


async def _reviewable_with_version(repos, service, set_id: str, product_id: str) -> ScriptVersion:
    item = await repos.items.get_by_product(set_id, product_id)
    version = ScriptVersion(
        id="script_version:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        script_item_id=item.id,
        version=1,
        spoken_text="x",
    )
    await repos.versions.insert(version)
    item.state = ScriptState.REVIEWABLE
    item.current_version_id = version.id
    return version


@pytest.mark.asyncio
async def test_approve_current_version_mismatch() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    await _reviewable_with_version(repos, service, created["id"], "P1")
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.approve_product(
            set_id=created["id"],
            product_id="P1",
            version_id="script_version:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            actor="nam",
        )
    assert exc.value.code == "illegal_transition"
    assert "only the current REVIEWABLE version" in exc.value.message


@pytest.mark.asyncio
async def test_approve_gate_fail_maps_illegal_transition() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_fail_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    version = await _reviewable_with_version(repos, service, created["id"], "P1")
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.approve_product(
            set_id=created["id"], product_id="P1", version_id=version.id, actor="nam"
        )
    assert exc.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_approve_stale_revision_mapping() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    version = await _reviewable_with_version(repos, service, created["id"], "P1")
    repos.items.fail_update = True
    with pytest.raises(ScriptAuthoringError) as exc:
        await service.approve_product(
            set_id=created["id"], product_id="P1", version_id=version.id, actor="nam"
        )
    assert exc.value.code == "stale_revision"


@pytest.mark.asyncio
async def test_recover_orchestrator_rebuilds_from_state() -> None:
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass_result())
    )
    created = await service.create_script_set(
        name="A", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    repos.batches = _RecoveryBatchRepo()
    orch, persist_queue, bridge = await service._recover_orchestrator("b1", created["id"])  # noqa: SLF001
    assert orch is not None
    assert persist_queue is not None
    assert bridge is not None
    assert "b1" in service._active_orchestrators
