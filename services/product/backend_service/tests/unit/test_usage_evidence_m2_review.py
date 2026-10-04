"""P0-FB-017 M1/M2 review round: a stop never waits for the database, deferred facts keep their
order, retained evidence outlives a long outage. Injected clocks and gates, no sleeps."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.api.v1 import execution as ex
from backend.application import budget_lease
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import RESCUE_SWITCH, Evidence, ExecutionIdentity
from backend.application.usage_evidence import envelope
from backend.application.usage_evidence.envelope import Draft

from . import test_approved_speech_active as speech_tests
from .test_budget_lease import T0, Rig
from .test_rescue_commands import send
from .test_usage_evidence_m2 import TENANT, MemOutbox, case_with, committed_row, wire

case_factory = speech_tests.case_factory
direct_say_guard_off = speech_tests.direct_say_guard_off
pytestmark = pytest.mark.timeout(30)
DAY = 24 * 3600


@pytest.fixture(autouse=True)
def rescue(monkeypatch):
    monkeypatch.setenv(RESCUE_SWITCH, "1")
    monkeypatch.delenv(budget_lease.ENV_ENABLED, raising=False)
    yield
    budget_lease.set_active(False)


def ident_for(sid, tenant=TENANT):
    return ExecutionIdentity(
        tenant_id=tenant, business_session_id="b", runtime_session_id=sid, generation="g"
    )


# -- P1-1: a stop never waits for the database ------------------------------------------------


def fenced(store):
    """A distributed-lock store whose fenced save is refused once ``lease_ok`` is False
    (the Redis lock expired: it has no renewal)."""
    from contextlib import asynccontextmanager

    state = SimpleNamespace(lease_ok=True)
    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def with_session_lock(sid, **_):
        async with locks.setdefault(sid, asyncio.Lock()):
            yield SimpleNamespace(session_id=sid, token="t")

    async def commit_if_owner(fence, data, ttl_seconds=None):
        if not state.lease_ok:
            return False
        await store.set(fence.session_id, data)
        return True

    store.with_session_lock, store.commit_if_owner = with_session_lock, commit_if_owner
    return state


def hang_and_lose_the_lease(lock_state):
    async def hook():  # the first database call is slower than the lock lease: never returns
        if not lock_state.lease_ok:
            raise ConnectionError("still down")
        lock_state.lease_ok = False
        await asyncio.Event().wait()

    return hook


async def test_emergency_end_is_saved_and_speech_cancelled_before_a_hanging_outbox_is_touched(
    case_factory, monkeypatch
):
    monkeypatch.setattr(ex, "DEFER_DRAIN_BUDGET", 0.05)
    case = await case_with(case_factory)
    lock_state = fenced(case.d.store)
    case.outbox.hook = hang_and_lose_the_lease(lock_state)
    out = await send(case, "emergency_end", "ee-slow", tenant_id=TENANT)
    assert out["outcome"]["status"] == "applied"  # not a 503 session_busy
    meta = await case.d.store.get(case.sid)
    assert meta["execution_contract"]["phase"] == "ending"
    assert len(meta["usage_evidence_unstaged"]) == 1
    assert case.d.approved_speech.blocked(case.sid) == "ending"  # speech stopped


async def test_forced_control_lost_terminates_and_stops_speech_with_a_hanging_outbox(
    case_factory, monkeypatch
):
    monkeypatch.setattr(ex, "DEFER_DRAIN_BUDGET", 0.05)
    case = await case_with(case_factory)
    lock_state = fenced(case.d.store)
    case.outbox.hook = hang_and_lose_the_lease(lock_state)
    blocks, real_block = [], case.d.approved_speech.block
    monkeypatch.setattr(
        case.d.approved_speech, "block", lambda sid, why: (blocks.append(why), real_block(sid, why))
    )
    rig = Rig(case)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    assert await rig.enforcer.sweep() == [case.sid]
    st, meta = await rig.phase()
    assert st.phase == "failed" and meta["usage_terminal_unstaged"]
    assert "ending" in blocks  # speech was stopped


# -- P2-3: deferred facts keep their order ----------------------------------------------------


def terminal_evidence(case, seq):
    return Evidence(
        tenant_id=TENANT,
        business_session_id="business-1",
        runtime_session_id=case.sid,
        generation="generation-1",
        sequence=seq,
        kind="terminal",
        phase="ended",
        reason_code="normal_end",
        occurred_at=T0,
    )


def request_of(case):
    from fastapi import Request

    return Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(container=case.d))}
    )


async def test_later_evidence_cannot_overtake_a_deferred_emergency_end_fact(case_factory):
    from fastapi import HTTPException

    case = await case_with(case_factory)
    case.outbox.fail = {"*"}
    await send(case, "emergency_end", "ee-order", tenant_id=TENANT)
    seq = (await case.d.store.get(case.sid))["execution_contract"]["sequence"]
    case.outbox.fail = set()  # Postgres is back, but the deferred fact is not staged yet
    with pytest.raises(HTTPException) as caught:
        await ex.record_execution_evidence(
            case.sid, terminal_evidence(case, seq + 1), request_of(case), None
        )
    assert caught.value.status_code == 503 and case.outbox.rows == {}
    assert (await case.d.store.get(case.sid))["execution_contract"]["phase"] == "ending"
    await case.d.usage_sender.sweep()  # the drain stages the earlier fact first ...
    await ex.record_execution_evidence(
        case.sid, terminal_evidence(case, seq + 1), request_of(case), None
    )  # ... then the later one is accepted
    assert case.outbox.numbered() == [(1, "phase_changed"), (2, "terminal")]


def drafts_and_entries(ident):
    drafts = [
        Draft("phase_changed", "phase:ending", "start", "1", "ending", T0, 6, 6),
        Draft("terminal", "terminal", "point", "2", "ended", T0, 7, 7),
    ]
    entries = [{"identity": ident.model_dump(), "draft": envelope.draft_to_dict(d)} for d in drafts]
    return entries, drafts


async def test_a_blocked_earlier_fact_holds_back_every_later_deferred_fact():
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox)
    ident = ident_for("rt-8")
    entries, drafts = drafts_and_entries(ident)
    await outbox.stage(ident, [drafts[0]], version=1)  # an unresolved attempt blocks fact #1
    await store.set("rt-8", {"usage_evidence_unstaged": entries})
    sender.track("rt-8")
    await sender.drain_session("rt-8")
    assert len((await store.get("rt-8"))["usage_evidence_unstaged"]) == 2  # nothing overtook it
    assert [r["kind"] for r in outbox.rows.values()] == ["phase_changed"]


# -- P1-2: retained evidence outlives a long outage -------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TtlStore(InMemorySessionStore):
    """A session store with expiry on an injected clock (the Redis 24 h default TTL)."""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.expiry = clock, {}

    async def get(self, sid):
        if sid in self.expiry and self.clock() >= self.expiry[sid]:
            await super().delete(sid)
            self.expiry.pop(sid)
        return await super().get(sid)

    async def set(self, sid, data, ttl_seconds=None):
        await super().set(sid, data)
        self.expiry[sid] = self.clock() + (ttl_seconds or DAY)

    async def ttl_remaining(self, sid):
        return self.expiry[sid] - self.clock() if sid in self.expiry else None


async def test_deferred_facts_survive_a_nine_day_outage_and_are_staged_on_recovery():
    clock = Clock()
    store, outbox = TtlStore(clock), MemOutbox()
    _, sender = wire(store, outbox)
    entries, _ = drafts_and_entries(ident_for("rt-9"))
    await ex._save(store, "rt-9", {"usage_evidence_unstaged": entries}, None)  # as the request does
    assert await store.ttl_remaining("rt-9") > 2 * DAY  # not the 24 h default
    sender.track("rt-9")
    outbox.fail = {"stage"}
    for _ in range(9 * 24):  # hourly sweeps while Postgres is down
        clock.t += 3600
        await sender.sweep()
    assert len((await store.get("rt-9"))["usage_evidence_unstaged"]) == 2
    outbox.fail = set()
    await sender.sweep()
    assert outbox.numbered() == [(1, "phase_changed"), (2, "terminal")]
    assert "usage_evidence_unstaged" not in await store.get("rt-9")


async def test_the_commit_proof_of_a_staged_row_survives_a_nine_day_outage():
    clock = Clock()
    store, outbox = TtlStore(clock), MemOutbox()
    _, sender = wire(store, outbox)
    st = await committed_row(outbox, store, "rt-10")  # default 24 h meta
    outbox.fail = {"mark_ready"}
    for _ in range(9 * 24):
        clock.t += 3600
        await sender.sweep()
    assert (await store.get("rt-10"))["usage_evidence_commits"]["tokens"]
    outbox.fail = set()
    await sender.sweep()
    assert outbox.rows[st.event_id]["status"] == "ready"


async def test_unresolved_evidence_near_expiry_is_an_error_log(caplog):
    clock = Clock()
    store, outbox = TtlStore(clock), MemOutbox()
    _, sender = wire(store, outbox)
    entries, _ = drafts_and_entries(ident_for("rt-11"))
    await store.set("rt-11", {"usage_evidence_unstaged": entries}, ttl_seconds=3 * 3600)
    sender.track("rt-11")
    outbox.fail = {"stage"}
    await sender.sweep()
    assert "UNRESOLVED usage evidence" in caplog.text
