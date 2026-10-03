"""P0-FB-017 review round 2: evidence survives a FULL outage, a lost fence aborts destructive
work, post-deadline attempts are detached, unstageable facts are given up after hours.

Outages are total (every outbox method raises); clocks and gates are injected, no sleeps."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.api.v1 import execution as ex
from backend.application import budget_lease
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import RESCUE_SWITCH
from backend.application.usage_evidence import envelope
from backend.application.usage_evidence.envelope import Draft

from . import test_approved_speech_active as speech_tests
from .test_budget_lease import T0, Rig
from .test_rescue_commands import send
from .test_usage_evidence_m2 import TENANT, MemOutbox, case_with, committed_row, wire
from .test_usage_evidence_m2_review import (
    DAY,
    Clock,
    TtlStore,
    drafts_and_entries,
    ident_for,
)

case_factory = speech_tests.case_factory
direct_say_guard_off = speech_tests.direct_say_guard_off
pytestmark = pytest.mark.timeout(30)


@pytest.fixture(autouse=True)
def rescue(monkeypatch):
    monkeypatch.setenv(RESCUE_SWITCH, "1")
    monkeypatch.delenv(budget_lease.ENV_ENABLED, raising=False)
    yield
    budget_lease.set_active(False)


async def sweep_quietly(sender):
    try:
        await sender.sweep()
    except Exception:  # the run loop logs and goes on; a full outage raises out of the sweep
        pass


# -- P1-1: a FULL outage cannot expire what is retained -----------------------------------------


async def test_a_full_outage_keeps_proofs_deferred_facts_and_cleanup_markers_for_nine_days():
    clock = Clock()
    store, outbox = TtlStore(clock), MemOutbox()
    _, sender = wire(store, outbox)
    await committed_row(outbox, store, "rt-A")  # ordinary commit proof, default 24 h meta
    entries, _ = drafts_and_entries(ident_for("rt-B"))
    await ex._save(store, "rt-B", {"usage_evidence_unstaged": entries}, None)  # deferred facts
    await committed_row(outbox, store, "rt-C")
    marked = await store.get("rt-C")
    marked["usage_evidence_cleanup"] = True  # Stop kept it
    await store.set("rt-C", marked)
    sender.track("rt-B")
    sender.track("rt-C")
    outbox.fail = {"*"}  # EVERY outbox operation raises, for the whole outage
    for _ in range(9 * 24):
        clock.t += 3600
        await sweep_quietly(sender)
    assert (await store.get("rt-A"))["usage_evidence_commits"]["tokens"]
    assert len((await store.get("rt-B"))["usage_evidence_unstaged"]) == 2
    kept = await store.get("rt-C")
    assert kept["usage_evidence_cleanup"] and kept["usage_evidence_commits"]["tokens"]
    outbox.fail = set()  # recovery converges everything
    await sender.sweep()
    await sender.sweep()
    assert {v["status"] for v in outbox.rows.values()} == {"ready"}
    assert await store.get("rt-C") is None  # clean: the marked meta is deleted


async def test_a_restarted_sender_in_a_full_outage_still_refreshes_what_it_discovers():
    clock = Clock()
    store, outbox = TtlStore(clock), MemOutbox()
    await committed_row(outbox, store, "rt-D")
    _, fresh = wire(store, outbox)  # a new process: nothing tracked in memory
    outbox.fail = {"*"}
    for _ in range(9 * 24):
        clock.t += 3600
        await sweep_quietly(fresh)
    assert (await store.get("rt-D"))["usage_evidence_commits"]["tokens"]


async def test_commit_proofs_alone_count_as_held_evidence_for_the_save_ttl():
    clock = Clock()
    store = TtlStore(clock)
    await ex._save(
        store, "rt-E", {"usage_evidence_commits": {"version": 1, "tokens": {"t": 1}}}, None
    )
    assert await store.ttl_remaining("rt-E") > 2 * DAY


# -- P1-2: losing the fence aborts destructive work ---------------------------------------------


class OwnedStore(InMemorySessionStore):
    """In-memory store with a real lock lease: ``steal`` models a newer owner taking over."""

    def __init__(self):
        super().__init__()
        self.owner: dict[str, str] = {}
        self.serial = 0
        self.on_ttl = None

    @asynccontextmanager
    async def with_session_lock(self, sid, **_):
        self.serial += 1
        token = f"lease-{self.serial}"
        self.owner[sid] = token
        try:
            yield SimpleNamespace(session_id=sid, token=token)
        finally:
            if self.owner.get(sid) == token:
                del self.owner[sid]

    def steal(self, sid, data):
        """The lease expired; another instance took the lock and persisted ``data``."""
        self.owner[sid] = "newer-owner"
        self._store[sid] = data

    async def commit_if_owner(self, fence, data, ttl_seconds=None):
        if self.owner.get(fence.session_id) != fence.token:
            return False
        await self.set(fence.session_id, data)
        return True

    async def delete_if_owner(self, fence):
        if self.owner.get(fence.session_id) != fence.token:
            return False
        return await self.delete(fence.session_id)

    async def ttl_remaining(self, sid):
        if self.on_ttl is not None:
            await self.on_ttl()
        return 100.0


async def test_a_lost_fence_aborts_resolution_before_ready_and_never_touches_newer_metadata():
    store, outbox = OwnedStore(), MemOutbox()
    _, sender = wire(store, outbox)
    st = await committed_row(outbox, store, "rt-F")
    mine = await store.get("rt-F")
    newer = {**mine, "usage_evidence_unstaged": [{"identity": {}, "draft": {}}]}

    async def lease_expires_and_a_newer_owner_persists_a_deferred_fact():
        store.steal("rt-F", newer)

    store.on_ttl = lease_expires_and_a_newer_owner_persists_a_deferred_fact
    (row,) = await outbox.staged_for_session("rt-F")
    await sender._resolve([row], 1e12)
    assert outbox.rows[st.event_id]["status"] == "staged"  # not marked ready from a stale view
    assert (await store.get("rt-F")) == newer  # the newer owner's metadata is untouched


async def test_the_cleanup_delete_is_ownership_protected():
    store, outbox = OwnedStore(), MemOutbox()
    _, sender = wire(store, outbox)
    await store.set("rt-G", {"usage_evidence_cleanup": True})
    sender.track("rt-G")
    newer = {"usage_evidence_cleanup": True, "usage_evidence_unstaged": [{"x": 1}]}
    real = outbox.staged_for_session

    async def lease_lost_during_the_check(sid):
        store.steal(sid, newer)
        return await real(sid)

    outbox.staged_for_session = lease_lost_during_the_check
    try:
        await sender.drain_session("rt-G")
    except Exception:
        pass
    assert (await store.get("rt-G")) == newer  # an unfenced delete would have destroyed it


async def test_the_stop_path_deletes_only_under_its_fence():
    from backend.api.v1 import sessions

    store = OwnedStore()
    await store.set("rt-H", {"status": "active"})
    d = SimpleNamespace(
        store=store, usage_sender=None, hub=None, locks=SimpleNamespace(drop=lambda s: None)
    )
    async with ex._locked(store, "rt-H") as fence:
        store.steal("rt-H", {"newer": True})  # the lease was lost before the delete
        with pytest.raises(Exception):
            await sessions.delete_session_meta(d, "rt-H", fence)
    assert await store.get("rt-H") == {"newer": True}


# -- P2-3: the staging attempt after a safety stop is a hard deadline ----------------------------


def hanging_cancellation(release):
    calls = []

    async def hook():
        if calls:  # only the FIRST call stalls; later ones (the completion) fail fast
            raise ConnectionError("still down")
        calls.append(1)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()  # the cancellation cleanup (pool release) itself stalls
            raise

    return hook


async def test_emergency_end_returns_at_the_deadline_even_if_cancellation_cleanup_hangs(
    case_factory, monkeypatch
):
    monkeypatch.setattr(ex, "DEFER_DRAIN_BUDGET", 0.05)
    case = await case_with(case_factory)
    release = asyncio.Event()
    case.outbox.hook = hanging_cancellation(release)
    out = await send(case, "emergency_end", "ee-hang", tenant_id=TENANT)
    assert out["outcome"]["status"] == "applied"  # returned; did not wait for the cleanup
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)


async def test_the_lease_expiry_attempt_is_a_hard_deadline_too(case_factory, monkeypatch):
    monkeypatch.setattr(ex, "DEFER_DRAIN_BUDGET", 0.05)
    case = await case_with(case_factory)
    # A Redis-like lock: it EXPIRES instead of excluding forever, so the stalled detached
    # attempt cannot deadlock the completion that follows (the memory lock would).
    leased = OwnedStore()
    leased._store = case.d.store._store
    case.d.store = leased
    case.d.usage_evidence, case.d.usage_sender = wire(leased, case.outbox)
    release = asyncio.Event()
    case.outbox.hook = hanging_cancellation(release)
    rig = Rig(case)
    await rig.lease(10)
    rig.now = T0 + timedelta(seconds=11)
    assert await rig.enforcer.sweep() == [case.sid]
    st, _ = await rig.phase()
    assert st.phase == "failed"
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)


# -- notes: bounded give-up, fake fidelity -------------------------------------------------------


async def test_a_permanently_blocked_fact_is_given_up_after_hours_and_later_facts_go_through(
    caplog,
):
    clock = Clock()
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox, clock=clock)
    ident = ident_for("rt-I")
    entries, drafts = drafts_and_entries(ident)
    for e in entries:
        e["deferred_at"] = clock()
    await outbox.stage(ident, [drafts[0]], version=1)  # an unresolved attempt blocks fact #1
    await store.set("rt-I", {"usage_evidence_unstaged": entries})
    sender.track("rt-I")
    clock.t += 3600
    await sender.drain_session("rt-I")
    assert len((await store.get("rt-I"))["usage_evidence_unstaged"]) == 2  # not yet
    clock.t += 24 * 3600
    await sender.drain_session("rt-I")
    assert "usage_evidence_unstaged" not in await store.get("rt-I")
    assert "UNSTAGEABLE" in caplog.text
    assert [r["kind"] for r in outbox.rows.values()] == ["phase_changed", "terminal"]


async def test_an_outage_never_triggers_the_give_up():
    clock = Clock()
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox, clock=clock)
    entries, _ = drafts_and_entries(ident_for("rt-J"))
    for e in entries:
        e["deferred_at"] = clock()
    await store.set("rt-J", {"usage_evidence_unstaged": entries})
    sender.track("rt-J")
    outbox.fail = {"stage"}
    clock.t += 30 * DAY
    await sender.drain_session("rt-J")
    assert len((await store.get("rt-J"))["usage_evidence_unstaged"]) == 2


async def test_the_fake_outbox_validates_identity_and_closes_only_a_committed_opening():
    outbox = MemOutbox()
    bad = ident_for("rt-K", tenant="tenant-1")
    with pytest.raises(envelope.InvalidIdentity):
        await outbox.stage(bad, [Draft("phase_changed", "p", "start", "1", "x", T0, 1, 1)])
    ident = ident_for("rt-L")
    start = Draft("unusable_started", "unusable", "start", "5", "x", T0, 5, 5)
    end = Draft("unusable_ended", "unusable", "end", None, "x", T0, 6, 6)
    (st,) = await outbox.stage(ident, [start], version=1)  # staged, NOT committed
    assert await outbox.stage(ident, [end], version=2, committed_tokens=set()) == []
    out = await outbox.stage(ident, [end], version=2, committed_tokens={st.token})
    assert len(out) == 1 and out[0].created
