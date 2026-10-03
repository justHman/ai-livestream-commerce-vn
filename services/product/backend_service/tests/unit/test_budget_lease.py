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


@pytest.fixture(autouse=True)
def stub_cleanup(request, monkeypatch):
    """Expiry tests inspect the failed state, so cleanup is down unless a test is named
    ``test_completion_*`` (those run the real /stop completion)."""
    if request.node.name.startswith("test_completion_"):
        return

    async def down(d, session_id):
        raise RuntimeError("cleanup stubbed down")

    monkeypatch.setattr("backend.api.v1.sessions.stop_session_internal", down)


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


# -- P0-FB-017 interplay: the forced end stages usage evidence like the 016 end path ------


@pytest.mark.asyncio
async def test_forced_end_stages_usage_evidence_exactly_once(case_factory):
    from .test_usage_evidence import FakeUsage

    case = await live(case_factory)
    usage = case.d.usage_evidence = FakeUsage()
    rig = Rig(case)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    assert await rig.enforcer.sweep() == [case.sid]
    st, meta = await rig.phase()
    assert st.phase == "failed" and len(usage.staged) == 1
    assert usage.staged == [st.sequence] and usage.committed == [f"e{st.sequence}"]
    assert meta["usage_evidence_commits"]["tokens"] == {f"t{st.sequence}": 1}
    assert await rig.enforcer.sweep() == []  # idempotent: nothing staged twice
    assert len(usage.staged) == 1


@pytest.mark.asyncio
async def test_forced_end_with_unavailable_outbox_still_terminates(case_factory):
    """Safety stop: a staging failure never blocks the termination (017 M2 follow-up)."""
    from .test_usage_evidence import FakeUsage

    case = await live(case_factory)
    usage = case.d.usage_evidence = FakeUsage(fail=True)
    rig = Rig(case)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    assert await rig.enforcer.sweep() == [case.sid]
    st, meta = await rig.phase()
    assert (
        st.phase == "failed" and meta["usage_terminal_unstaged"]["evidence"]["kind"] == "terminal"
    )
    assert usage.staged == [] and usage.committed == []


# -- P1-1: lease expiry and Hold are independent restrictions ------------------------------


async def expired_rig(case_factory):
    case = await live(case_factory)
    rig = Rig(case, speaking=True)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    return case, rig


def refuses(case):
    with pytest.raises(SpeechRejected) as caught:
        case.d.approved_speech.check_start(case.sid)
    return str(caught.value)


@pytest.mark.asyncio
async def test_renewal_clears_only_the_lease_restriction_never_hold(case_factory):
    case, rig = await expired_rig(case_factory)
    speech = case.d.approved_speech
    speech.block(case.sid, "held")  # merchant Hold
    await rig.enforcer.sweep()  # expiry on top of Hold
    await rig.lease(60, sequence=2)  # renewal
    await rig.enforcer.sweep()
    assert refuses(case) == "held"


@pytest.mark.asyncio
async def test_resume_clears_only_hold_never_the_expired_lease_even_before_a_sweep(case_factory):
    case, rig = await expired_rig(case_factory)
    speech = case.d.approved_speech
    speech.block(case.sid, "held")
    await rig.enforcer.sweep()
    speech.block(case.sid, None)  # Resume, no sweep in between
    assert refuses(case) == "lease_expired"


@pytest.mark.asyncio
async def test_both_restrictions_clear_independently_in_either_order(case_factory):
    case, rig = await expired_rig(case_factory)
    speech = case.d.approved_speech
    speech.block(case.sid, "held")
    await rig.enforcer.sweep()
    await rig.lease(60, sequence=2)
    await rig.enforcer.sweep()
    speech.block(case.sid, None)
    speech.check_start(case.sid)  # both cleared: a turn may start
    # expiry only, then renewal
    await rig.lease(10, sequence=3)
    rig.now = T0 + timedelta(seconds=999)
    await rig.enforcer.sweep()
    assert refuses(case) == "lease_expired"


# -- P1-3: control loss completes cleanup and the durable terminal record ------------------


async def terminal_rig(case_factory, fail_with=None):
    from backend.application.terminal_outcomes import TerminalOutcomes

    from .test_terminal_outcomes import FakePg

    case = await live(case_factory)
    pg = FakePg(fail_with=fail_with)
    case.d.terminal_outcomes = TerminalOutcomes(pg)
    rig = Rig(case)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    return case, rig, pg


@pytest.mark.asyncio
async def test_completion_control_lost_writes_one_terminal_record_and_cleans_up(case_factory):
    case, rig, pg = await terminal_rig(case_factory)
    assert await rig.enforcer.sweep() == [case.sid]
    assert pg.calls == ["persist"] and len(pg.stored) == 1
    (record,) = pg.stored.values()
    assert (record.terminal_phase, record.failure_class) == ("failed", "control_lost")
    assert await case.d.store.get(case.sid) is None  # the /stop completion deleted it
    assert await rig.enforcer.sweep() == []  # repeated sweeps: nothing more
    late = await case.client.post(f"/api/v1/sessions/{case.sid}/stop", headers=ADMIN)
    assert late.status_code in (404, 409)
    assert pg.calls == ["persist"]


@pytest.mark.asyncio
async def test_completion_retries_after_a_failing_persist_without_a_double_record(case_factory):
    from backend.application.terminal_outcomes import TerminalPersistError

    case, rig, pg = await terminal_rig(case_factory, fail_with=TerminalPersistError("db"))
    assert await rig.enforcer.sweep() == [case.sid]  # failed is saved, completion pending
    meta = await case.d.store.get(case.sid)
    assert meta["execution_lease_termination"] == "pending" and pg.stored == {}
    pg.fail_with = None  # the store recovers
    await rig.enforcer.sweep()  # a later sweep retries the completion
    assert len(pg.stored) == 1 and await case.d.store.get(case.sid) is None
    await rig.enforcer.sweep()
    assert len(pg.stored) == 1


@pytest.mark.asyncio
async def test_reason_code_end_stages_command_evidence_once(case_factory, direct_say_guard_off):
    from .test_usage_evidence import FakeUsage

    class Usage(FakeUsage):
        calls: list = []

        async def stage_command(self, prior, updated, outcome, meta):
            self.calls.append((outcome.command, outcome.end_reason))
            return []

    case = await live(case_factory)
    usage = case.d.usage_evidence = Usage()
    usage.calls = []
    await command(case, "end", actor_id=SYSTEM, reason_code="entitlement_exhausted")
    assert usage.calls == [("end", "entitlement_exhausted")]


# -- auth planes on the commands route (real dependencies, DISTINCT tokens configured) ----

VIEWER = {"Authorization": "Bearer viewer-secret"}


async def tokened(case_factory, monkeypatch):
    case = await live(case_factory)
    monkeypatch.setattr(case.d.config, "app_env", "prod", raising=False)
    monkeypatch.setattr(case.d.config, "backend_api_token", "viewer-secret", raising=False)
    monkeypatch.setattr(case.d.config, "admin_api_token", "admin-secret", raising=False)
    return case


@pytest.mark.asyncio
async def test_system_end_with_only_the_admin_token_succeeds(
    case_factory, direct_say_guard_off, monkeypatch
):
    case = await tokened(case_factory, monkeypatch)
    for name in ("end",):
        r = await command(
            case, name, headers=ADMIN, actor_id=SYSTEM, reason_code="entitlement_exhausted"
        )
        assert applied(r, name)["end_reason"] == "entitlement_exhausted"


@pytest.mark.asyncio
async def test_system_emergency_end_with_only_the_admin_token_succeeds(
    case_factory, direct_say_guard_off, monkeypatch
):
    case = await tokened(case_factory, monkeypatch)
    r = await command(
        case, "emergency_end", headers=ADMIN, actor_id=SYSTEM, reason_code="entitlement_exhausted"
    )
    assert applied(r, "emergency_end")["end_reason"] == "entitlement_exhausted"


@pytest.mark.asyncio
async def test_reason_code_is_403_with_the_viewer_token_or_a_non_system_actor(
    case_factory, direct_say_guard_off, monkeypatch
):
    case = await tokened(case_factory, monkeypatch)
    r = await command(
        case,
        "end",
        status=403,
        headers=VIEWER,
        actor_id=SYSTEM,
        reason_code="entitlement_exhausted",
    )
    assert r["error"]["code"] == "reason_code_forbidden"
    r = await command(
        case,
        "end",
        status=403,
        headers=ADMIN,
        actor_id="owner-1",
        reason_code="entitlement_exhausted",
    )
    assert r["error"]["code"] == "reason_code_forbidden"


@pytest.mark.asyncio
async def test_commands_without_reason_code_work_with_either_token_and_401_otherwise(
    case_factory, direct_say_guard_off, monkeypatch
):
    case = await tokened(case_factory, monkeypatch)
    await command(case, "hold", headers=VIEWER)  # unchanged merchant path
    await command(case, "resume", headers=ADMIN)  # admin without reason_code == viewer
    for headers in ({}, {"Authorization": "Bearer nope"}):
        await command(case, "hold", status=401, headers=headers, command_id="h2")


# -- round 2 ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_keeps_the_session_until_the_unstaged_terminal_fact_is_staged(
    case_factory,
):
    from .test_usage_evidence import FakeUsage

    case, rig, pg = await terminal_rig(case_factory)
    usage = case.d.usage_evidence = FakeUsage(fail=True)
    assert await rig.enforcer.sweep() == [case.sid]  # failed is saved regardless
    meta = await case.d.store.get(case.sid)
    assert meta["usage_terminal_unstaged"] and pg.stored == {}  # NOT cleaned up yet
    assert len(rig.enforcer.unstaged_pending) == 1  # backlog / health counter
    await rig.enforcer.sweep()  # inside the backoff window: no hammering
    assert usage.staged == [] and await case.d.store.get(case.sid) is not None
    usage.fail = False
    rig.now += timedelta(seconds=60)  # injected clock past the backoff
    await rig.enforcer.sweep()
    assert len(usage.staged) == 1 and usage.committed == [f"e{usage.staged[0]}"]
    assert len(pg.stored) == 1 and await case.d.store.get(case.sid) is None
    assert rig.enforcer.unstaged_pending == {}
    await rig.enforcer.sweep()
    assert len(usage.staged) == 1 and len(pg.stored) == 1


@pytest.mark.asyncio
async def test_completion_survives_an_unknown_backend_session_and_persists_one_record(
    case_factory,
):
    import asyncio

    from backend.application.terminal_outcomes import TerminalPersistError

    case, rig, pg = await terminal_rig(case_factory, fail_with=TerminalPersistError("db"))
    await asyncio.to_thread(case.d.backend.stop, case.sid)  # released before the restart
    await rig.enforcer.sweep()  # failed saved; completion hits KeyError, then persist fails
    meta = await case.d.store.get(case.sid)
    assert meta["execution_lease_termination"] == "pending" and pg.stored == {}
    pg.fail_with = None
    await rig.enforcer.sweep()
    assert len(pg.stored) == 1 and await case.d.store.get(case.sid) is None
    await rig.enforcer.sweep()
    assert len(pg.stored) == 1


@pytest.mark.asyncio
async def test_completion_unknown_session_without_a_pending_marker_is_still_404(case_factory):
    case = await live(case_factory)
    r = await case.client.post("/api/v1/sessions/nope/stop", headers=ADMIN)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_failed_hold_restores_only_hold_state_not_the_expired_lease(
    case_factory, monkeypatch
):
    from fastapi import HTTPException

    case, rig = await expired_rig(case_factory)  # speaking: gate holds, no end yet
    await rig.enforcer.sweep()
    real = case.d.store.set

    async def refusing(key, value):
        if value.get("execution_contract", {}).get("hold", {}).get("held"):
            raise HTTPException(status_code=503, detail={"code": "session_busy"})
        await real(key, value)

    monkeypatch.setattr(case.d.store, "set", refusing)
    await command(case, "hold", status=503)
    monkeypatch.setattr(case.d.store, "set", real)
    assert case.d.approved_speech.held_reason(case.sid) is None
    await rig.lease(60, sequence=2)  # renewal
    await rig.enforcer.sweep()
    case.d.approved_speech.check_start(case.sid)  # allowed


@pytest.mark.asyncio
async def test_expired_persisted_lease_refuses_the_first_turn_without_any_sweep(case_factory):
    case = await live(case_factory)
    rig = Rig(case)
    await rig.lease(10)
    speech = case.d.approved_speech
    speech.set_lease_expiry(case.sid, None)  # a fresh process: nothing in memory
    rig.now = T0 + timedelta(seconds=11)
    speech.check_start(case.sid)  # before rehydrate the service knows nothing
    await rig.make().rehydrate()  # lifespan start, before traffic
    assert refuses(case) == "lease_expired"  # no sweep ran
    await rig.lease(60, sequence=2)
    await rig.enforcer.sweep()  # renewal re-allows
    speech.check_start(case.sid)


@pytest.mark.asyncio
async def test_the_admission_gate_flips_exactly_at_the_expiry_instant(case_factory):
    case = await live(case_factory)
    rig = Rig(case)
    speech = case.d.approved_speech
    speech.set_lease_expiry(case.sid, T0 + timedelta(seconds=10))
    rig.now = T0 + timedelta(seconds=10) - timedelta(microseconds=1)
    speech.check_start(case.sid)
    rig.now = T0 + timedelta(seconds=10)
    assert refuses(case) == "lease_expired"
