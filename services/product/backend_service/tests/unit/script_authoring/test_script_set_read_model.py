"""HIGH-2 RED: production read model for human review/approval (TDD RED first).

The reviewer flagged that ``GET /script-sets/{id}`` strips every field a human
reviewer needs to actually approve — version id, version content, gate state.
These tests fail until ``_set_wire`` is enriched and ``get_script_set`` hydrates
the per-item current version. Contract tests used a fake that fabricated versions,
so they did not surface the stripping; this suite exercises the REAL service.

Three cases:
  1. item wire AFTER a draft exposes ``current_version_id``/``current_version``/``gate``;
  2. item wire with NO version yet is NULL-safe;
  3. approval E2E reads the exact spoken_text via the read wire and approves it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from backend.application.script_authoring.gate.results import (
    GateRunResult,
    RuleSetFingerprint,
    RuleViolation,
    Severity,
)
from backend.application.script_authoring.models import (
    GateRun,
    ScriptItem,
    ScriptSet,
    ScriptVersion,
)
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig


class _FakeGate:
    def __init__(self, result: GateRunResult) -> None:
        self._result = result

    def run_full_script(self, segments, context):
        return self._result

    def run_segment(self, text, context):
        return self._result


def _pass() -> GateRunResult:
    return GateRunResult(scope="full_script", fingerprint=RuleSetFingerprint())


def _fail(msg="fails") -> GateRunResult:
    return GateRunResult(
        scope="full_script",
        fingerprint=RuleSetFingerprint(),
        violations=(RuleViolation(rule_id="R", severity=Severity.ERROR, message=msg),),
    )


# Minimal fake repos — mirror those in test_service_impl_core.py so the
# assertion shapes are identical. The key point: `_set_wire` must surface
# current_version_id / approved_version_id / current_version / gate; today it
# only surfaces {"state": ...} and these tests fail.


class _FakeSetRepo:
    def __init__(self):
        self.rows: dict[str, ScriptSet] = {}

    async def get(self, set_id: str, *, conn=None):
        return self.rows.get(set_id)

    async def insert(self, set_: ScriptSet, *, conn=None):
        self.rows[set_.id] = set_

    async def update(self, set_: ScriptSet, *, expected_revision: int, conn=None):
        self.rows[set_.id] = set_


class _FakeItemRepo:
    def __init__(self):
        self.rows: dict[str, ScriptItem] = {}

    async def get(self, item_id: str, *, conn=None):
        return self.rows.get(item_id)

    async def get_by_product(self, set_id: str, product_id: str, *, conn=None):
        return next(
            (
                i
                for i in self.rows.values()
                if i.script_set_id == set_id and i.product_id == product_id
            ),
            None,
        )

    async def list_by_set(self, set_id: str, *, conn=None):
        return [i for i in self.rows.values() if i.script_set_id == set_id]

    async def insert(self, item: ScriptItem, *, conn=None):
        self.rows[item.id] = item

    async def update(self, item: ScriptItem, *, expected_revision: int, conn=None):
        self.rows[item.id] = item


class _FakeVersionRepo:
    def __init__(self):
        self.rows: dict[str, ScriptVersion] = {}

    async def get(self, version_id: str, *, conn=None):
        v = self.rows.get(version_id)
        return v.model_copy(deep=True) if v is not None else None

    async def insert(self, version: ScriptVersion, *, conn=None):
        self.rows[version.id] = version.model_copy(deep=True)

    async def list_by_item(self, item_id: str, *, conn=None):
        return list(self.rows.values())


class _FakeGateRunRepo:
    def __init__(self):
        self.rows: dict[str, GateRun] = {}

    async def insert(self, run: GateRun, *, conn=None):
        self.rows[run.id] = run

    async def list_by_item(self, item_id: str, *, conn=None):
        return [r for r in self.rows.values() if r.script_item_id == item_id]

    async def latest_for_version(self, version_id: str, *, conn=None):
        return None


class _FakeApprovalRepo:
    def __init__(self):
        self.rows: dict = {}

    async def insert(self, approval, *, dependencies: dict, conn=None):
        self.rows[approval.id] = approval


class _FakeBatchRepo:
    async def get(self, batch_id: str, *, conn=None):
        return None

    async def insert(self, batch, *, state, conn=None):
        pass

    async def find_by_idempotency(self, *a, **kw):
        return None


class _FakeJobRepo:
    async def find_by_idempotency(self, *a, **kw):
        return None

    async def insert(self, job, *, conn=None):
        pass


class _FakeIdem:
    async def get(self, *a, **kw):
        return None

    async def register(self, *a, **kw):
        pass


class _FakePlanRepo:
    async def insert(self, *a, **kw):
        pass


class _FakeSegmentRepo:
    async def insert(self, *a, **kw):
        pass


class _FakeRepos:
    def __init__(self):
        self.script_sets = _FakeSetRepo()
        self.items = _FakeItemRepo()
        self.versions = _FakeVersionRepo()
        self.gate_runs = _FakeGateRunRepo()
        self.approvals = _FakeApprovalRepo()
        self.plans = _FakePlanRepo()
        self.segments = _FakeSegmentRepo()
        self.batches = _FakeBatchRepo()
        self.jobs = _FakeJobRepo()
        self.idempotency = _FakeIdem()

    @asynccontextmanager
    async def transaction(self):
        yield object()


@pytest.mark.asyncio
async def test_read_wire_exposes_current_version_after_draft() -> None:
    """After a draft, GET must surface current_version_id + current_version + gate."""
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass())
    )
    created = await service.create_script_set(
        name="S", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    set_id = created["id"]
    await service.save_draft(
        set_id=set_id,
        product_id="P1",
        display_text="Hello 100k",
        spoken_text="Hello 100k spoken",
        revision=None,
    )
    wire = await service.get_script_set(set_id=set_id)
    assert wire is not None
    item = wire["items"]["P1"]
    # Must NOT be just {"state": ...}
    assert "current_version_id" in item, f"read wire missing current_version_id: {item}"
    assert "approved_version_id" in item
    assert "current_version" in item
    assert "gate" in item
    assert item["current_version_id"] is not None
    cv = item["current_version"]
    assert cv is not None
    assert cv["id"] == item["current_version_id"]
    assert cv["spoken_text"] == "Hello 100k spoken"
    assert cv["display_text"] == "Hello 100k"
    assert cv["source"] in ("manual", "MANUAL", "Manual") or "manual" in str(cv["source"]).lower()


@pytest.mark.asyncio
async def test_read_wire_null_when_no_version_yet() -> None:
    """EMPTY/DRAFT-before-any-version: current_version_id/current_version/approved_version_id are null."""
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass())
    )
    created = await service.create_script_set(
        name="S", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    wire = await service.get_script_set(set_id=created["id"])
    assert wire is not None
    item = wire["items"]["P1"]
    assert item["current_version_id"] is None
    assert item["current_version"] is None
    assert item["approved_version_id"] is None


@pytest.mark.asyncio
async def test_approve_e2e_via_read_wire_exact_text_binding() -> None:
    """Production E2E: read the exact spoken_text from the wire, then approve it."""
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos, config=ScriptAuthoringConfig(), gate=_FakeGate(_pass())
    )
    created = await service.create_script_set(
        name="S", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    set_id = created["id"]
    SPOKEN = "Xin chao quy khach, san pham nay gia 299k."
    await service.save_draft(
        set_id=set_id, product_id="P1", display_text=SPOKEN, spoken_text=SPOKEN, revision=None
    )
    # Submit to reach REVIEWABLE so approval is legal; the fake gate passes.
    submitted = await service.submit_for_gate(set_id=set_id, product_id="P1")
    assert submitted["state"] == "REVIEWABLE"
    wire = await service.get_script_set(set_id=set_id)
    assert wire is not None
    item = wire["items"]["P1"]
    cv = item["current_version"]
    assert cv is not None
    assert cv["spoken_text"] == SPOKEN
    version_id = item["current_version_id"]
    approved = await service.approve_product(
        set_id=set_id, product_id="P1", version_id=version_id, actor="nam"
    )
    assert approved["state"] == "APPROVED"
    wire2 = await service.get_script_set(set_id=set_id)
    assert wire2["items"]["P1"]["approved_version_id"] == version_id
    assert wire2["items"]["P1"]["current_version"]["spoken_text"] == SPOKEN
