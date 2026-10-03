"""P0-FB-018 S6: budget-lease route, expiry watcher and FLAG-018-1 reason_code.

Real HTTP endpoint, real store and approved-speech fence; the clock and the
utterance-in-flight probe are injected, so nothing here sleeps."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.application import budget_lease
from backend.application.budget_lease import BudgetLeaseEnforcer, LeaseSettings
from backend.application.execution_contract import (
    RESCUE_SWITCH,
    Capabilities,
    Cleanup,
    Evidence,
    ExecutionState,
    apply_evidence,
)
from backend.application.script_authoring.approved_speech import SpeechRejected
from backend.application.terminal_outcomes import build_terminal_record

from . import test_approved_speech_active as speech_tests
from .test_rescue_commands import applied, body, live, send, state

case_factory = speech_tests.case_factory
direct_say_guard_off = speech_tests.direct_say_guard_off

pytestmark = pytest.mark.timeout(30)
ADMIN = {"Authorization": "Bearer admin-secret"}
T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
SYSTEM = budget_lease.SYSTEM_ACTOR
COMMANDS = "/api/v1/sessions/{}/execution/commands"


@pytest.fixture(autouse=True)
def rescue(monkeypatch):
    monkeypatch.setenv(RESCUE_SWITCH, "1")
    monkeypatch.delenv(budget_lease.ENV_ENABLED, raising=False)
    yield
    budget_lease.set_active(False)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def lease_body(case, sequence=1, expires=None, lease_id="lease-1", **identity):
    ident = {
        "tenant_id": "tenant-1",
        "business_session_id": "business-1",
        "runtime_session_id": case.sid,
        "generation": "generation-1",
        **identity,
    }
    expires = expires or iso(datetime.now(timezone.utc) + timedelta(seconds=60))
    return {"identity": ident, "lease_id": lease_id, "sequence": sequence, "expires_at": expires}


async def push(case, status=200, headers=ADMIN, **kw):
    r = await case.client.post(
        f"/api/v1/sessions/{case.sid}/execution/budget-lease",
        json=lease_body(case, **kw),
        headers=headers,
    )
    assert r.status_code == status, r.text
    return r.json()


async def command(case, name, status=200, headers=ADMIN, **changes):
    r = await case.client.post(
        COMMANDS.format(case.sid), json=body(case, name, **changes), headers=headers
    )
    assert r.status_code == status, r.text
    return r.json()


# --- route -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_route_is_409_and_capability_absent(case_factory):
    case = await live(case_factory)
    assert (await push(case, status=409))["error"]["code"] == "budget_lease_not_enabled"
    assert "budget.lease.v1" not in (await state(case))["capabilities"]["available"]
    assert "budget.lease.v1" not in Capabilities().available


@pytest.mark.asyncio
async def test_capability_advertised_only_when_active(case_factory):
    case = await live(case_factory)
    budget_lease.set_active(True)
    assert "budget.lease.v1" in (await state(case))["capabilities"]["available"]
    budget_lease.set_active(False)
    assert "budget.lease.v1" not in (await state(case))["capabilities"]["available"]


@pytest.mark.asyncio
async def test_lease_accept_replay_lower_stale_and_bad_expiry(case_factory):
    case = await live(case_factory)
    budget_lease.set_active(True)
    first = await push(case, sequence=2, lease_id="lease-2")
    assert first["applied"] is True and first["lease"]["sequence"] == 2
    meta = await case.d.store.get(case.sid)
    assert meta["execution_budget_lease"]["lease_id"] == "lease-2"

    for seq in (2, 1):  # replay and lower sequence: idempotent, stored lease returned
        again = await push(case, sequence=seq, lease_id="other")
        assert again["applied"] is False and again["lease"]["lease_id"] == "lease-2"
    assert (await case.d.store.get(case.sid))["execution_budget_lease"] == first["lease"]

    stale = await push(case, status=409, sequence=9, generation="generation-0")
    assert stale["error"]["code"] == "stale_generation"
    assert (await case.d.store.get(case.sid))["execution_budget_lease"]["sequence"] == 2

    now = datetime.now(timezone.utc)
    for bad in (
        iso(now - timedelta(seconds=60)),  # expired in the past (beyond skew)
        iso(now + timedelta(days=2)),  # beyond the max horizon
        "2026-10-03T12:00:00",  # no offset
        "not-a-date",
    ):
        r = await push(case, status=422, sequence=5, expires=bad)
        assert r["error"]["code"] == "invalid_expires_at"
    assert (await case.d.store.get(case.sid))["execution_budget_lease"]["sequence"] == 2


@pytest.mark.asyncio
async def test_lease_within_skew_tolerance_is_accepted(case_factory):
    case = await live(case_factory)
    budget_lease.set_active(True)
    near = iso(datetime.now(timezone.utc) - timedelta(seconds=1))
    assert (await push(case, expires=near))["applied"] is True


@pytest.mark.asyncio
async def test_lease_route_requires_admin_token(case_factory):
    case = await live(case_factory)
    budget_lease.set_active(True)
    await push(case, status=401, headers={})
    await push(case, status=401, headers={"Authorization": "Bearer wrong"})
    assert "execution_budget_lease" not in await case.d.store.get(case.sid)


@pytest.mark.asyncio
async def test_lease_rejected_after_terminal(case_factory):
    case = await live(case_factory, phase="failed")
    budget_lease.set_active(True)
    assert (await push(case, status=409))["error"]["code"] == "already_terminal"


# --- expiry watcher --------------------------------------------------------


class Rig:
    def __init__(self, case, speaking=False, wait=30.0):
        self.now = T0
        self.speaking = speaking
        self.case = case
        self.settings = LeaseSettings(enabled=True, safe_boundary_wait=wait)
        self.enforcer = self.make()

    def make(self):
        return BudgetLeaseEnforcer(
            self.case.d,
            self.settings,
            clock=lambda: self.now,
            candidates=lambda: {self.case.sid},
            is_speaking=lambda sid: self.speaking,
        )

    async def lease(self, seconds, sequence=1):
        meta = await self.case.d.store.get(self.case.sid)
        meta["execution_budget_lease"] = {
            "lease_id": "l",
            "sequence": sequence,
            "expires_at": budget_lease.rfc3339(T0 + timedelta(seconds=seconds)),
        }
        await self.case.d.store.set(self.case.sid, meta)

    async def phase(self):
        meta = await self.case.d.store.get(self.case.sid)
        return ExecutionState.model_validate(meta["execution_contract"]), meta


@pytest.mark.asyncio
async def test_unexpired_lease_changes_nothing(case_factory):
    case = await live(case_factory)
    rig = Rig(case)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=9)
    assert await rig.enforcer.sweep() == []
    assert case.d.approved_speech.blocked(case.sid) is None
    assert (await rig.phase())[0].phase == "selling"


@pytest.mark.asyncio
async def test_expiry_holds_gate_then_ends_at_safe_boundary(case_factory):
    case = await live(case_factory)
    rig = Rig(case, speaking=True)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    assert await rig.enforcer.sweep() == []
    assert case.d.approved_speech.blocked(case.sid) == "lease_expired"
    with pytest.raises(SpeechRejected):
        case.d.approved_speech.check_start(case.sid)
    assert (await rig.phase())[0].phase == "selling"  # utterance still finishing

    rig.speaking = False  # safe boundary reached
    assert await rig.enforcer.sweep() == [case.sid]
    st, meta = await rig.phase()
    assert (st.phase, st.terminal_reason) == ("failed", "execution_failed")
    assert meta["execution_failure_class"] == "control_lost"
    assert case.d.approved_speech.blocked(case.sid) == "ending"
    record = build_terminal_record(meta, now=T0, cleanup=Cleanup(status="succeeded"))
    assert (record.terminal_phase, record.reason_code, record.failure_class) == (
        "failed",
        "execution_failed",
        "control_lost",
    )
    assert await rig.enforcer.sweep() == []  # idempotent


@pytest.mark.asyncio
async def test_forced_end_after_the_safe_boundary_bound(case_factory):
    case = await live(case_factory)
    rig = Rig(case, speaking=True, wait=30.0)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    await rig.enforcer.sweep()
    rig.now = T0 + timedelta(seconds=40)  # 29 s after the gate
    assert await rig.enforcer.sweep() == []
    epoch = case.d.approved_speech._epochs.get(case.sid, 0)
    rig.now = T0 + timedelta(seconds=41)  # 30 s after the gate: end anyway
    assert await rig.enforcer.sweep() == [case.sid]
    assert (await rig.phase())[0].phase == "failed"
    assert case.d.approved_speech._epochs.get(case.sid, 0) > epoch  # late audio fenced


@pytest.mark.asyncio
async def test_renewal_inside_the_wait_releases_the_gate(case_factory):
    case = await live(case_factory)
    rig = Rig(case, speaking=True)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    await rig.enforcer.sweep()
    await rig.lease(60, sequence=2)
    assert await rig.enforcer.sweep() == []
    assert case.d.approved_speech.blocked(case.sid) is None
    assert (await rig.phase())[0].phase == "selling"


@pytest.mark.asyncio
async def test_resume_cannot_reopen_the_gate_while_expired(case_factory):
    case = await live(case_factory)
    rig = Rig(case, speaking=True)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    await rig.enforcer.sweep()
    case.d.approved_speech.block(case.sid, None)
    await rig.enforcer.sweep()
    assert case.d.approved_speech.blocked(case.sid) == "lease_expired"


@pytest.mark.asyncio
async def test_restart_resumes_from_persisted_meta(case_factory):
    case = await live(case_factory)
    rig = Rig(case)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=500)  # expired while the process was down
    fresh = rig.make()  # no in-memory state at all
    assert await fresh.sweep() == [case.sid]
    assert (await rig.phase())[1]["execution_failure_class"] == "control_lost"


@pytest.mark.asyncio
async def test_not_enforced_before_live_or_after_end(case_factory):
    for phase in ("ready", "closing", "ending"):
        case = await live(case_factory, phase=phase)
        rig = Rig(case)
        await rig.lease(10)
        rig.now = T0 + timedelta(seconds=99)
        assert await rig.enforcer.sweep() == []
        assert (await rig.phase())[0].phase == phase


@pytest.mark.asyncio
async def test_session_without_lease_is_never_ended(case_factory):
    case = await live(case_factory)
    rig = Rig(case)
    rig.now = T0 + timedelta(days=1)
    assert await rig.enforcer.sweep() == []


# --- FLAG-018-1 reason_code ------------------------------------------------


@pytest.mark.asyncio
async def test_system_end_carries_entitlement_exhausted(case_factory, direct_say_guard_off):
    case = await live(case_factory)
    result = await command(case, "end", actor_id=SYSTEM, reason_code="entitlement_exhausted")
    outcome = applied(result, "end")
    assert outcome["end_reason"] == "entitlement_exhausted"
    meta = await case.d.store.get(case.sid)
    record = build_terminal_record(meta, now=T0, cleanup=Cleanup(status="succeeded"))
    assert (record.terminal_phase, record.reason_code) == ("ended", "entitlement_exhausted")
    assert record.command_ref.actor_id == SYSTEM


@pytest.mark.asyncio
async def test_system_emergency_end_carries_reason(case_factory, direct_say_guard_off):
    case = await live(case_factory)
    result = await command(
        case, "emergency_end", actor_id=SYSTEM, reason_code="entitlement_exhausted"
    )
    assert applied(result, "emergency_end")["end_reason"] == "entitlement_exhausted"
    meta = await case.d.store.get(case.sid)
    record = build_terminal_record(meta, now=T0, cleanup=Cleanup(status="succeeded"))
    assert record.reason_code == "entitlement_exhausted"


@pytest.mark.asyncio
async def test_reason_code_rejected_for_everyone_else_and_audited(
    case_factory, direct_say_guard_off, caplog
):
    case = await live(case_factory)
    # merchant actor, even with the admin credential
    r = await command(
        case, "end", status=403, actor_id="owner-1", reason_code="entitlement_exhausted"
    )
    assert r["error"]["code"] == "reason_code_forbidden"
    assert "audit_event=reason_code_rejected" in caplog.text
    # right actor name but without the admin plane (viewer-plane spoof)
    r = await command(
        case,
        "end",
        status=403,
        headers={},
        actor_id=SYSTEM,
        reason_code="entitlement_exhausted",
    )
    assert r["error"]["code"] == "reason_code_forbidden"
    # right principal, wrong reason / wrong command
    r = await command(case, "end", status=422, actor_id=SYSTEM, reason_code="normal_end")
    assert r["error"]["code"] == "invalid_reason_code"
    r = await command(
        case, "hold", status=422, actor_id=SYSTEM, reason_code="entitlement_exhausted"
    )
    assert r["error"]["code"] == "reason_code_not_allowed"
    st = (await state(case))["state"]
    assert st["phase"] == "selling" and st["sequence"] == 3  # nothing applied


@pytest.mark.asyncio
async def test_absent_reason_code_is_unchanged_and_replay_binds_reason(
    case_factory, direct_say_guard_off
):
    case = await live(case_factory)
    outcome = applied(await send(case, "end", actor_id=SYSTEM), "end")
    assert outcome["end_reason"] is None
    meta = await case.d.store.get(case.sid)
    record = build_terminal_record(meta, now=T0, cleanup=Cleanup(status="succeeded"))
    assert record.reason_code == "normal_end"
    # the same command_id replayed with a reason is a different intent
    r = await command(case, "end", status=409, actor_id=SYSTEM, reason_code="entitlement_exhausted")
    assert r["error"]["code"] == "duplicate_command_conflict"


def test_apply_accepts_entitlement_exhausted_for_ended():
    ident = dict(tenant_id="t", business_session_id="b", runtime_session_id="r", generation="g")
    st = ExecutionState(**ident, sequence=4, phase="ending")
    ev = dict(**ident, sequence=5, kind="terminal", phase="ended", occurred_at=T0)
    done = apply_evidence(st, Evidence(**ev, reason_code="entitlement_exhausted"))
    assert done.terminal_reason == "entitlement_exhausted"
    with pytest.raises(Exception):
        apply_evidence(st, Evidence(**ev, reason_code="made_up"))


def test_runtime_computes_no_credits_balances_or_billable_time():
    root = Path(budget_lease.__file__).resolve().parents[1]
    banned = ("credit", "balance", "billable")
    offenders = []
    for path in root.rglob("*.py"):
        if {"clients", "text_chunker"} & set(path.parts):  # vendor SDK and unrelated parsing
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = (
                getattr(node, "id", None)
                or getattr(node, "name", None)
                or getattr(node, "arg", None)
            )
            if isinstance(name, str) and any(b in name.lower() for b in banned):
                offenders.append((path.name, name))
    assert offenders == []
