"""P0-FB-017 M1/M2 follow-up against a REAL disposable PostgreSQL (see test_usage_evidence_pg).

M1: the real ``_stop_cancelled_session`` must not delete the session meta (the only commit proof)
while usage rows are unresolved. M2: Emergency End is never refused by a staging failure; its
facts are recorded in the meta with the state and staged later. A database outage is simulated
by an outbox proxy; a clock is never slept on (``SWEEP_AGE`` is ~0, gates are explicit).
"""

# ruff: noqa: F811  (imported pytest fixtures are redefined as test arguments)
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.api.v1 import execution as ex
from backend.api.v1.sessions import _stop_cancelled_session
from backend.application import budget_lease
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import RESCUE_SWITCH, ExecutionState
from backend.application.usage_evidence import UsageEvidence, UsageEvidenceSettings, UsageSender
from backend.application.usage_evidence.outbox import UsageOutbox

from unit.test_budget_lease import Rig, T0
from unit.test_rescue_commands import live, send

from .test_usage_evidence_pg import (  # noqa: F401  (pg is a fixture)
    FAST,
    NOW,
    draft,
    meta_at,
    new_identity,
    pg,
    rows,
    URL,
)
from unit import test_approved_speech_active as speech_tests

case_factory = speech_tests.case_factory
direct_say_guard_off = speech_tests.direct_say_guard_off

pytestmark = [
    pytest.mark.skipif(not URL, reason="needs USAGE_EVIDENCE_TEST_DATABASE_URL"),
    pytest.mark.timeout(60),
]
TENANT = str(uuid.uuid4())
QUICK = FAST | {"USAGE_EVIDENCE_SWEEP_AGE_SECONDS": "0.000001"}


@pytest.fixture(autouse=True)
def rescue(monkeypatch):
    monkeypatch.setenv(RESCUE_SWITCH, "1")
    monkeypatch.delenv(budget_lease.ENV_ENABLED, raising=False)
    yield
    budget_lease.set_active(False)


class Outage:
    """Outbox proxy: ``fail`` names the methods that raise (``'*'`` = a full database outage)."""

    def __init__(self, real: UsageOutbox):
        self._real, self.fail = real, set()

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        async def call(*a, **kw):
            if name in self.fail or "*" in self.fail:
                raise ConnectionError("postgres is down")
            return await attr(*a, **kw)

        return call


def wire(pg, store, **env):
    """The real facade + sender on one outbox proxy, sharing the session lock like lifespan."""
    settings = UsageEvidenceSettings.from_env(QUICK | env)
    outbox = Outage(UsageOutbox(pg))
    service = UsageEvidence(outbox, settings)
    sender = UsageSender(
        outbox,
        settings,
        session_store=store,
        session_lock=lambda sid: ex._locked(store, sid),
        unstaged_sessions=service.unstaged_sessions,
    )
    return outbox, service, sender


async def pg_case(case_factory, pg):
    case = await live(case_factory)
    meta = await case.d.store.get(case.sid)
    meta["execution_contract"]["tenant_id"] = TENANT
    await case.d.store.set(case.sid, meta)
    outbox, service, sender = wire(pg, case.d.store)
    case.d.usage_evidence, case.d.usage_sender = service, sender
    case.outbox = outbox
    case.ident = ExecutionState.model_validate(meta["execution_contract"])
    return case


async def usage_rows(pg, case):
    async with pg._require_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT kind, status, usage_sequence FROM usage_evidence_outbox "
            "WHERE runtime_session_id = $1 ORDER BY created_at, kind",
            case.sid,
        )


def d_for(store, **extra):
    """The few container members ``_stop_cancelled_session`` touches (no media, no director)."""
    from types import SimpleNamespace

    class Backend:
        def stop(self, sid):
            return None

    class Locks:
        def drop(self, sid):
            return None

    return SimpleNamespace(
        store=store,
        orchestrators={},
        backend=Backend(),
        livekit_publishers=None,
        director=None,
        hub=None,
        locks=Locks(),
        **extra,
    )


# -- M1 --------------------------------------------------------------------------------------


async def test_real_stop_with_mark_ready_failing_keeps_the_meta_then_the_sweeper_flips_and_deletes(
    pg,
):
    ident, store = new_identity(), InMemorySessionStore()
    sid = ident.runtime_session_id
    outbox, service, sender = wire(pg, store)
    (st,) = await service._outbox.stage(ident, [draft(seq=5)], version=1)  # staged + committed...
    await store.set(sid, meta_at(ident, 5, [st.token]))  # ...but the ready flip never happened
    outbox.fail = {"mark_ready"}
    d = d_for(store, usage_sender=sender)

    async with ex._locked(store, sid) as fence:
        out = await _stop_cancelled_session(d, sid, fence)
    assert out == {"ok": True, "stopped": sid}

    kept = await store.get(sid)
    assert kept is not None and kept["usage_evidence_cleanup"] is True  # the proof survived
    assert kept["usage_evidence_commits"]["tokens"] == {st.token: 1}
    assert [r["status"] for r in await rows(pg, ident)] == ["staged"]

    outbox.fail = set()  # Postgres is back: the sweeper flips ready, then deletes the meta
    await sender.sweep()
    assert [(r["status"], r["usage_sequence"]) for r in await rows(pg, ident)] == [("ready", 1)]
    assert await store.get(sid) is None
    await sender.sweep()  # idempotent
    assert len(await rows(pg, ident)) == 1


async def test_stop_with_the_outbox_fully_down_keeps_the_meta_and_a_clean_stop_deletes_it(pg):
    ident, store = new_identity(), InMemorySessionStore()
    sid = ident.runtime_session_id
    outbox, service, sender = wire(pg, store)
    d = d_for(store, usage_sender=sender)
    (st,) = await service._outbox.stage(ident, [draft(seq=5)], version=1)
    await store.set(sid, meta_at(ident, 5, [st.token]))
    outbox.fail = {"*"}
    async with ex._locked(store, sid) as fence:
        await _stop_cancelled_session(d, sid, fence)  # resolve RAISES: treated as unresolved
    assert (await store.get(sid))["usage_evidence_cleanup"] is True
    outbox.fail = set()
    await sender.sweep()
    assert await store.get(sid) is None

    # with nothing unresolved the stop deletes at once and sets no marker
    other, store2 = new_identity(), InMemorySessionStore()
    _, _, sender2 = wire(pg, store2)
    await store2.set(other.runtime_session_id, meta_at(other, 1))
    async with ex._locked(store2, other.runtime_session_id) as fence:
        await _stop_cancelled_session(
            d_for(store2, usage_sender=sender2), other.runtime_session_id, fence
        )
    assert await store2.get(other.runtime_session_id) is None


# -- M2 --------------------------------------------------------------------------------------


async def test_emergency_end_with_postgres_down_is_applied_and_its_evidence_appears_exactly_once(
    case_factory, pg
):
    case = await pg_case(case_factory, pg)
    case.outbox.fail = {"*"}
    sent = {"tenant_id": TENANT}
    out = await send(case, "emergency_end", "ee-1", **sent)
    assert out["outcome"]["status"] == "applied" and out["replayed"] is False
    meta = await case.d.store.get(case.sid)
    assert meta["execution_contract"]["phase"] == "ending"
    assert len(meta["usage_evidence_unstaged"]) == 1  # recorded WITH the state, atomically
    assert await usage_rows(pg, case) == []

    case.outbox.fail = set()  # recovery
    await case.d.usage_sender.sweep()
    assert [(r["kind"], r["status"]) for r in await usage_rows(pg, case)] == [
        ("phase_changed", "ready")
    ]
    meta = await case.d.store.get(case.sid)
    assert "usage_evidence_unstaged" not in meta  # dropped in the same save as the proof

    replay = await send(case, "emergency_end", "ee-1", **sent)
    assert replay["replayed"] is True
    await case.d.usage_sender.sweep()
    await case.d.usage_sender.sweep()
    assert len(await usage_rows(pg, case)) == 1  # still exactly one


async def test_a_restarted_sender_discovers_deferred_facts_through_list_session_ids(
    case_factory, pg
):
    case = await pg_case(case_factory, pg)
    case.outbox.fail = {"*"}
    await send(case, "emergency_end", "ee-2", tenant_id=TENANT)
    case.outbox.fail = set()
    _, _, fresh = wire(pg, case.d.store)  # a new process: its in-memory set is empty
    await fresh.sweep()
    assert [r["status"] for r in await usage_rows(pg, case)] == ["ready"]


async def test_plain_end_stays_fail_closed_with_postgres_down_and_works_after_recovery(
    case_factory, pg
):
    case = await pg_case(case_factory, pg)
    case.outbox.fail = {"*"}
    await send(case, "end", "end-1", status=503, tenant_id=TENANT)
    meta = await case.d.store.get(case.sid)
    assert meta["execution_contract"]["phase"] == "selling"  # not applied
    assert "usage_evidence_unstaged" not in meta and "end-1" not in meta.get(
        "execution_command_outcomes", {}
    )
    case.outbox.fail = set()
    done = await send(case, "end", "end-1", tenant_id=TENANT)
    assert done["outcome"]["status"] == "applied"
    again = await send(case, "end", "end-1", tenant_id=TENANT)
    assert again["replayed"] is True
    for task in list(ex._closing_tasks.values()):  # the closing completion is a real task
        await task
    # closing + ending (the completion), each exactly once; the replay staged nothing
    assert [(r["status"], r["usage_sequence"]) for r in await usage_rows(pg, case)] == [
        ("ready", 1),
        ("ready", 2),
    ]


async def test_emergency_end_then_stop_before_recovery_keeps_the_meta_until_it_is_staged(
    case_factory, pg
):
    case = await pg_case(case_factory, pg)
    case.outbox.fail = {"*"}
    await send(case, "emergency_end", "ee-3", tenant_id=TENANT)
    async with ex._locked(case.d.store, case.sid) as fence:
        await _stop_cancelled_session(case.d, case.sid, fence)
    kept = await case.d.store.get(case.sid)
    assert kept["usage_evidence_cleanup"] and len(kept["usage_evidence_unstaged"]) == 1
    case.outbox.fail = set()
    await case.d.usage_sender.sweep()
    assert [r["status"] for r in await usage_rows(pg, case)] == ["ready"]
    assert await case.d.store.get(case.sid) is None  # the fact is staged: the meta may go


# -- the 018 forced control_lost path and this mechanism never double-stage -------------------


async def test_completion_forced_control_lost_and_the_deferred_drain_stage_one_row(
    case_factory, pg
):
    case = await pg_case(case_factory, pg)
    rig = Rig(case)
    meta = await case.d.store.get(case.sid)
    meta["execution_budget_lease"] = {
        "lease_id": "l",
        "sequence": 1,
        "expires_at": budget_lease.rfc3339(T0 + timedelta(seconds=10)),
    }
    await case.d.store.set(case.sid, meta)
    rig.now = T0 + timedelta(seconds=11)
    case.outbox.fail = {"*"}
    assert await rig.enforcer.sweep() == [case.sid]  # terminated although staging is down
    meta = await case.d.store.get(case.sid)
    assert meta["usage_terminal_unstaged"] and "usage_evidence_unstaged" not in meta
    assert await usage_rows(pg, case) == []

    case.outbox.fail = set()
    await case.d.usage_sender.sweep()  # the 017 drain has nothing of 018's
    assert await usage_rows(pg, case) == []
    rig.now += timedelta(seconds=120)  # past the watcher's own backoff
    await rig.enforcer.sweep()
    await case.d.usage_sender.sweep()
    await rig.enforcer.sweep()
    assert [(r["kind"], r["status"]) for r in await usage_rows(pg, case)] == [("terminal", "ready")]
    assert await case.d.store.get(case.sid) is None


# -- L2: park backoff caps -------------------------------------------------------------------


async def test_park_caps_transient_reasons_at_300s_and_proof_unavailable_at_one_hour(pg):
    ident_a, ident_b = new_identity(), new_identity()
    outbox = UsageOutbox(pg)
    store = InMemorySessionStore()
    _, _, sender = wire(pg, store, USAGE_EVIDENCE_SWEEP_AGE_SECONDS="2000")
    await outbox.stage(ident_a, [draft(seq=5)])
    await outbox.stage(ident_b, [draft(seq=5)])
    await store.set(ident_a.runtime_session_id, meta_at(ident_a, 5))
    # ident_b has no meta at all: proof_unavailable. ident_a's meta becomes unreadable:

    real_get = store.get

    async def flaky(sid):
        if sid == ident_a.runtime_session_id:
            raise ConnectionError("redis down")
        return await real_get(sid)

    store.get = flaky
    due = [
        r for i in (ident_a, ident_b) for r in await outbox.staged_for_session(i.runtime_session_id)
    ]
    import time as _t

    await sender._resolve(due, _t.monotonic() + 30)
    async with pg._require_pool().acquire() as conn:
        waits = {
            r["runtime_session_id"]: r["wait"]
            for r in await conn.fetch(
                "SELECT runtime_session_id, EXTRACT(EPOCH FROM (next_attempt_at - NOW()))::float8 "
                "AS wait FROM usage_evidence_outbox"
            )
        }
    assert 290 <= waits[ident_a.runtime_session_id] <= 300  # session_unreadable: capped at 300 s
    assert 1990 <= waits[ident_b.runtime_session_id] <= 2000  # proof_unavailable: the 1 h cap


# -- L4: purge repeats the status guard in the outer DELETE ----------------------------------


async def test_purge_never_deletes_a_row_that_was_revived_while_the_delete_waited(pg):
    ident, outbox = new_identity(), UsageOutbox(pg)
    (st,) = await outbox.stage(ident, [draft(seq=5)])
    async with pg._require_pool().acquire() as a, pg._require_pool().acquire() as probe:
        await a.execute(
            "UPDATE usage_evidence_outbox SET status = 'discarded', "
            "updated_at = NOW() - interval '30 days'"
        )
        tx = a.transaction()
        await tx.start()
        await a.fetch("SELECT 1 FROM usage_evidence_outbox FOR UPDATE")  # hold the row lock
        purge = asyncio.create_task(outbox.purge(1, 100))  # selects the discarded row, blocks
        for _ in range(2000):  # condition poll, not a timed sleep
            blocked = await probe.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query LIKE 'DELETE FROM usage_evidence_outbox%'"
            )
            if blocked:
                break
            await asyncio.sleep(0)
        assert blocked
        await a.execute("UPDATE usage_evidence_outbox SET status = 'staged', updated_at = NOW()")
        await tx.commit()
        assert await purge == 0
    assert [r["status"] for r in await rows(pg, ident)] == ["staged"]
    assert st.event_id


async def test_later_evidence_cannot_overtake_a_deferred_emergency_end_fact_in_postgres(
    case_factory, pg
):
    from fastapi import HTTPException, Request

    from backend.application.execution_contract import Evidence

    case = await pg_case(case_factory, pg)
    case.outbox.fail = {"*"}
    await send(case, "emergency_end", "ee-order", tenant_id=TENANT)
    seq = (await case.d.store.get(case.sid))["execution_contract"]["sequence"]
    case.outbox.fail = set()
    request = Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(container=case.d))}
    )
    later = Evidence(
        tenant_id=TENANT,
        business_session_id="business-1",
        runtime_session_id=case.sid,
        generation="generation-1",
        sequence=seq + 1,
        kind="terminal",
        phase="ended",
        reason_code="normal_end",
        occurred_at=NOW,
    )
    with pytest.raises(HTTPException) as caught:
        await ex.record_execution_evidence(case.sid, later, request, None)
    assert caught.value.status_code == 503 and await usage_rows(pg, case) == []
    await case.d.usage_sender.sweep()
    await ex.record_execution_evidence(case.sid, later, request, None)
    assert [(r["kind"], r["usage_sequence"]) for r in await usage_rows(pg, case)] == [
        ("phase_changed", 1),
        ("terminal", 2),
    ]
