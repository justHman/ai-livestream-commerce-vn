"""P0-FB-017 M1/M2 follow-up with an in-memory outbox (the real-Postgres twin is
tests/integration/test_usage_evidence_m2_pg.py). Real endpoint, real facade, real sender and
the real ``_stop_cancelled_session``; only the outbox is a controlled double. No sleeps."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.v1 import execution as ex
from backend.api.v1 import sessions
from backend.application import budget_lease
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import RESCUE_SWITCH, ExecutionIdentity
from backend.application.usage_evidence import UsageEvidence, UsageEvidenceSettings, UsageSender
from backend.application.usage_evidence.outbox import Staged, StageBlocked

from . import test_approved_speech_active as speech_tests
from .test_budget_lease import T0, Rig
from .test_rescue_commands import live, send

case_factory = speech_tests.case_factory
direct_say_guard_off = speech_tests.direct_say_guard_off
pytestmark = pytest.mark.timeout(30)
TENANT = str(uuid.uuid4())
ENV = {
    "USAGE_EVIDENCE_ENABLED": "1",
    "USAGE_EVIDENCE_SECRET": "unit-secret-not-a-credential",
    "USAGE_EVIDENCE_URL": "http://127.0.0.1:9/webhooks/ai/events",
}


@pytest.fixture(autouse=True)
def rescue(monkeypatch):
    monkeypatch.setenv(RESCUE_SWITCH, "1")
    monkeypatch.delenv(budget_lease.ENV_ENABLED, raising=False)
    yield
    budget_lease.set_active(False)


class MemOutbox:
    """UsageOutbox semantics copied from the Postgres implementation (outbox.py):

    - one row per semantic key (identity, kind, interval); an existing STAGED row is never
      replaced (StageBlocked), a DISCARDED tombstone is revived with a fresh token, any other
      status is final and returned unchanged (token None); a call is all-or-nothing;
    - ``mark_ready`` matches the attempt token on staged/discarded rows (a discarded one is
      revived) and numbers rows per identity in ``staged_version`` order, but a row waits while
      another still-staged row of the identity has a LOWER ``staged_version``.
    """

    ID = ("tenant_id", "business_session_id", "runtime_session_id", "generation")

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.counters: dict[tuple, int] = {}
        self.fail: set[str] = set()
        self.parks: list[tuple[list[str], str, float]] = []
        self.tokens = 0
        self.hook = None  # awaited at the start of stage/still_staged (slow-database tests)

    async def _check(self, name):
        if self.hook is not None and name in ("stage", "still_staged"):
            await self.hook()
        if name in self.fail or "*" in self.fail:
            raise ConnectionError("postgres is down")

    @staticmethod
    def _key(identity, d):
        return (
            *(getattr(identity, k) for k in MemOutbox.ID),
            d.kind,
            d.interval_kind,
            d.opening_ref,
        )

    async def stage(self, identity, drafts, *, version=1, committed_tokens=frozenset()):
        await self._check("stage")
        out, writes = [], {}
        for d in drafts:
            key = self._key(identity, d)
            eid = hashlib.sha256(repr(key).encode()).hexdigest()[:24]
            row = self.rows.get(eid)
            if row is not None and row["status"] not in ("staged", "discarded"):
                out.append(Staged(eid, row["status"], row["usage_sequence"], False))
                continue
            if row is not None and row["status"] == "staged":
                raise StageBlocked(eid)
            self.tokens += 1
            token = f"tok{self.tokens}"
            writes[eid] = {
                "event_id": eid,
                **identity.model_dump(),
                "applied_sequence": d.applied_sequence,
                "status": "staged",
                "stage_token": token,
                "staged_version": int(version),
                "usage_sequence": None,
                "attempts": 0,
                "age_seconds": 0.0,
                "kind": d.kind,
            }
            out.append(Staged(eid, "staged", None, True, token, int(version)))
        self.rows.update(writes)  # all or nothing
        return out

    def _idk(self, row):
        return tuple(row[k] for k in self.ID)

    async def mark_ready(self, items):
        await self._check("mark_ready")
        tokens = dict(items)
        rows = [
            r
            for r in self.rows.values()
            if r["event_id"] in tokens
            and r["status"] in ("staged", "discarded")
            and r["stage_token"] == tokens[r["event_id"]]
        ]
        flipped = []
        horizon = {}
        for r in rows:
            k = self._idk(r)
            others = [
                o["staged_version"]
                for o in self.rows.values()
                if self._idk(o) == k
                and o["status"] == "staged"
                and o["event_id"] not in {x["event_id"] for x in rows}
            ]
            horizon[k] = min(others) if others else None
        for r in sorted(rows, key=lambda r: (r["staged_version"], r["event_id"])):
            k = self._idk(r)
            if horizon[k] is not None and r["staged_version"] > horizon[k]:
                continue  # an earlier unresolved row must be numbered first
            self.counters[k] = self.counters.get(k, 0) + 1
            r.update(status="ready", usage_sequence=self.counters[k])
            flipped.append(r["event_id"])
        return flipped

    async def still_staged(self, tokens):
        await self._check("still_staged")
        return {r["stage_token"] for r in self.rows.values() if r["status"] == "staged"} & set(
            tokens
        )

    async def staged_for_session(self, sid):
        await self._check("staged_for_session")
        return [
            dict(r)
            for r in sorted(self.rows.values(), key=lambda r: r["staged_version"])
            if r["status"] == "staged" and r["runtime_session_id"] == sid
        ]

    async def get_staged(self, eid):
        await self._check("get_staged")
        row = self.rows.get(eid)
        return dict(row) if row else None

    async def release(self, items, *, delete):
        await self._check("release")
        n = 0
        for eid, token in items:
            row = self.rows[eid]
            if row["stage_token"] == token and row["status"] == "staged":
                row["status"] = "discarded"
                row["usage_sequence"] = None
                n += 1
        return n

    async def stale_staged(self, age, limit=100):
        await self._check("stale_staged")
        return [dict(r) for r in self.rows.values() if r["status"] == "staged"][:limit]

    async def park(self, ids, *, reason, retry_in, cap=3600.0):
        self.parks.append((list(ids), reason, cap))
        return len(ids)

    async def defer(self, ids, *, base, cap):
        return len(ids)

    async def purge(self, days, limit):
        return 0

    def statuses(self):
        return sorted((r["kind"], r["status"]) for r in self.rows.values())

    def numbered(self):
        return sorted(
            (r["usage_sequence"], r["kind"]) for r in self.rows.values() if r["status"] == "ready"
        )


def wire(store, outbox, **env):
    settings = UsageEvidenceSettings.from_env(ENV | env)
    service = UsageEvidence(outbox, settings)
    sender = UsageSender(
        outbox,
        settings,
        session_store=store,
        session_lock=lambda sid: ex._locked(store, sid),
        unstaged_sessions=service.unstaged_sessions,
    )
    return service, sender


async def case_with(case_factory):
    case = await live(case_factory)
    meta = await case.d.store.get(case.sid)
    meta["execution_contract"]["tenant_id"] = TENANT
    await case.d.store.set(case.sid, meta)
    case.outbox = MemOutbox()
    case.d.usage_evidence, case.d.usage_sender = wire(case.d.store, case.outbox)
    return case


# -- M2 --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_end_is_applied_with_the_outbox_down_and_staged_once_after_recovery(
    case_factory,
):
    case = await case_with(case_factory)
    case.outbox.fail = {"*"}
    out = await send(case, "emergency_end", "ee-1", tenant_id=TENANT)
    assert out["outcome"]["status"] == "applied"
    meta = await case.d.store.get(case.sid)
    assert meta["execution_contract"]["phase"] == "ending"
    assert len(meta["usage_evidence_unstaged"]) == 1 and case.outbox.rows == {}

    case.outbox.fail = set()
    await case.d.usage_sender.sweep()
    assert case.outbox.statuses() == [("phase_changed", "ready")]
    meta = await case.d.store.get(case.sid)
    assert "usage_evidence_unstaged" not in meta and meta["usage_evidence_commits"]["tokens"]

    assert (await send(case, "emergency_end", "ee-1", tenant_id=TENANT))["replayed"] is True
    await case.d.usage_sender.sweep()
    assert case.outbox.statuses() == [("phase_changed", "ready")]  # still exactly one


@pytest.mark.asyncio
async def test_a_blocked_drain_keeps_the_fact_and_a_restarted_sender_finds_it(case_factory):
    case = await case_with(case_factory)
    case.outbox.fail = {"stage"}
    await send(case, "emergency_end", "ee-2", tenant_id=TENANT)
    await case.d.usage_sender.sweep()  # still down: the fact stays in the meta
    assert len((await case.d.store.get(case.sid))["usage_evidence_unstaged"]) == 1
    case.outbox.fail = set()
    _, fresh = wire(case.d.store, case.outbox)  # a new process: empty in-memory set
    await fresh.sweep()
    assert case.outbox.statuses() == [("phase_changed", "ready")]


@pytest.mark.asyncio
async def test_plain_end_stays_fail_closed_503_with_the_outbox_down(case_factory):
    case = await case_with(case_factory)
    case.outbox.fail = {"*"}
    await send(case, "end", "end-1", status=503, tenant_id=TENANT)
    meta = await case.d.store.get(case.sid)
    assert meta["execution_contract"]["phase"] == "selling"
    assert "usage_evidence_unstaged" not in meta


@pytest.mark.asyncio
async def test_an_undeliverable_identity_never_blocks_an_emergency_end(case_factory):
    case = await case_with(case_factory)  # tenant "tenant-1" would be non-UUID: use it
    meta = await case.d.store.get(case.sid)
    meta["execution_contract"]["tenant_id"] = "tenant-1"
    await case.d.store.set(case.sid, meta)
    out = await send(case, "emergency_end", "ee-3")
    assert out["outcome"]["status"] == "applied"
    assert "usage_evidence_unstaged" not in await case.d.store.get(case.sid)


def test_the_closing_completion_backoff_outlasts_the_sweepers_resolve_window():
    window = UsageEvidenceSettings().sweep_age + UsageEvidenceSettings().sweep_budget
    total = sum(min(30.0, ex.CLOSING_BACKOFF * 2**a) for a in range(ex.CLOSING_ATTEMPTS))
    assert total > 2 * window


# -- M1 --------------------------------------------------------------------------------------


def d_for(store, sender):
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
        usage_sender=sender,
    )


async def committed_row(outbox, store, sid):
    ident = ExecutionIdentity(
        tenant_id=TENANT, business_session_id="biz", runtime_session_id=sid, generation="g1"
    )
    from backend.application.usage_evidence.envelope import Draft

    (st,) = await outbox.stage(
        ident,
        [Draft("phase_changed", "phase:selling", "start", "", "selling", T0, 5, 5)],
        version=1,
    )
    from backend.application.execution_contract import ExecutionState

    await store.set(
        sid,
        {
            "execution_contract": ExecutionState(**ident.model_dump(), sequence=5).model_dump(
                mode="json"
            ),
            "usage_evidence_commits": {"version": 1, "tokens": {st.token: 1}},
        },
    )
    return st


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", [{"mark_ready"}, {"*"}])
async def test_stop_keeps_the_meta_while_rows_are_unresolved_and_the_sweeper_deletes_it(failing):
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox)
    st = await committed_row(outbox, store, "rt-1")
    outbox.fail = failing
    async with ex._locked(store, "rt-1") as fence:
        await sessions._stop_cancelled_session(d_for(store, sender), "rt-1", fence)
    kept = await store.get("rt-1")
    assert kept["usage_evidence_cleanup"] is True and kept["usage_evidence_commits"]["tokens"]
    outbox.fail = set()
    await sender.sweep()
    assert outbox.rows[st.event_id]["status"] == "ready" and await store.get("rt-1") is None


@pytest.mark.asyncio
async def test_stop_deletes_at_once_when_nothing_is_unresolved():
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox)
    await store.set("rt-2", {"status": "active"})
    async with ex._locked(store, "rt-2") as fence:
        await sessions._stop_cancelled_session(d_for(store, sender), "rt-2", fence)
    assert await store.get("rt-2") is None


@pytest.mark.asyncio
async def test_resolve_session_reports_what_remains_instead_of_swallowing_it():
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox)
    await committed_row(outbox, store, "rt-3")
    outbox.fail = {"mark_ready"}
    with pytest.raises(ConnectionError):
        await sender.resolve_session("rt-3")
    outbox.fail = set()
    assert await sender.resolve_session("rt-3") == 0


# -- 018 interplay -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_forced_control_lost_and_the_deferred_drain_stage_one_row(case_factory):
    case = await case_with(case_factory)
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
    assert await rig.enforcer.sweep() == [case.sid]
    meta = await case.d.store.get(case.sid)
    assert meta["usage_terminal_unstaged"] and "usage_evidence_unstaged" not in meta
    case.outbox.fail = set()
    await case.d.usage_sender.sweep()
    assert case.outbox.rows == {}  # the 017 drain owns nothing of 018's fact
    rig.now += timedelta(seconds=120)
    await rig.enforcer.sweep()
    await case.d.usage_sender.sweep()
    await rig.enforcer.sweep()
    assert case.outbox.statuses() == [("terminal", "ready")]
    assert await case.d.store.get(case.sid) is None


# -- L2 / L3 / L4 --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_park_cap_is_300s_for_transient_reasons_and_one_hour_for_proof_unavailable():
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox)
    await committed_row(outbox, store, "rt-4")  # readable
    await committed_row(outbox, store, "rt-5")
    await store.delete("rt-5")  # session_missing -> proof_unavailable
    real_get = store.get

    async def unreadable(sid):
        if sid == "rt-4":
            raise ConnectionError("redis down")
        return await real_get(sid)

    store.get = unreadable
    rows = [*await outbox.staged_for_session("rt-4"), *await outbox.staged_for_session("rt-5")]
    await sender._resolve(rows, time.monotonic() + 30)
    caps = {reason: cap for _, reason, cap in outbox.parks}
    assert caps == {"session_unreadable": 300.0, "proof_unavailable:session_missing": 3600.0}


@pytest.mark.asyncio
async def test_session_meta_writers_wait_for_the_session_lock():
    store = InMemorySessionStore()
    await store.set("rt-6", {"status": "active"})
    d = SimpleNamespace(store=store)
    async with ex._locked(store, "rt-6"):
        writer = asyncio.create_task(sessions._update_meta(d, "rt-6", run_plan={"x": 1}))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not writer.done() and "run_plan" not in await store.get("rt-6")
    await writer
    assert (await store.get("rt-6"))["run_plan"] == {"x": 1}


def test_the_purge_outer_delete_repeats_the_status_guard():
    src = (
        Path(sessions.__file__).parents[2] / "application" / "usage_evidence" / "outbox.py"
    ).read_text("utf-8")
    start = src.index('"DELETE FROM usage_evidence_outbox "')
    assert (
        "status IN ('delivered', 'conflict', 'rejected', 'discarded') " in src[start : start + 160]
    )


@pytest.mark.asyncio
async def test_cleanup_never_deletes_a_meta_that_still_holds_018s_unstaged_terminal_fact():
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox)
    meta = {"usage_evidence_cleanup": True, budget_lease.UNSTAGED_KEY: {"prior": {}}}
    await store.set("rt-7", meta)
    await sender._cleanup_if_done("rt-7", meta)
    assert await store.get("rt-7") is not None
    meta.pop(budget_lease.UNSTAGED_KEY)
    await sender._cleanup_if_done("rt-7", meta)
    assert await store.get("rt-7") is None
