"""Crash recovery across replicas after the surviving sender finished discovery."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.api.v1 import execution as ex, sessions
from backend.application import budget_lease as bl
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import ExecutionState

from .test_usage_evidence_m2 import MemOutbox, wire
from .test_usage_evidence_m2_review import drafts_and_entries, ident_for


async def test_running_sender_discovers_facts_saved_later_by_a_crashed_replica():
    store, outbox = InMemorySessionStore(), MemOutbox()
    _, sender = wire(store, outbox)
    await sender.sweep()  # initial discovery completed before the other writer exists
    entries, _ = drafts_and_entries(ident_for("rt-later"))
    await store.set(
        "rt-later", {"usage_evidence_unstaged": entries, "usage_evidence_cleanup": True}
    )
    # Writer dies after the atomic save: no track() call and no restart of this sender.
    await sender.sweep()
    assert len(outbox.rows) == 2
    assert all(r["status"] == "ready" for r in outbox.rows.values())
    assert await store.get("rt-later") is None


async def test_every_hanging_staging_retry_leaves_watcher_free_to_hard_stop_other_sessions(
    monkeypatch,
):
    store = InMemorySessionStore()
    now = datetime.now(timezone.utc)
    release = asyncio.Event()
    database_tasks = set()
    hard_stops = []

    class HangingEvidence:
        async def stage_evidence(self, *args):
            database_tasks.add(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()  # even connection/pool cancellation never returns
                raise

    async def hard_cancel(d, sid):
        hard_stops.append(sid)

    async def teardown(*args):
        pass  # unrelated 019 persistence; keep real stop/keep_for_unstaged code

    monkeypatch.setattr(ex, "DEFER_DRAIN_BUDGET", 0.01)
    monkeypatch.setattr(ex, "hard_cancel", hard_cancel)
    monkeypatch.setattr(sessions, "teardown_then_persist", teardown)
    for sid in ("A", "B"):
        state = ExecutionState(
            tenant_id=str(uuid.uuid4()),
            business_session_id="business",
            runtime_session_id=sid,
            generation="g",
            phase="selling",
            sequence=3,
        )
        await store.set(
            sid,
            {
                "execution_contract": state.model_dump(mode="json"),
                bl.LEASE_KEY: {"expires_at": bl.rfc3339(now - timedelta(seconds=1))},
            },
        )
    d = SimpleNamespace(
        store=store,
        usage_evidence=HangingEvidence(),
        usage_sender=None,
        approved_speech=SimpleNamespace(
            block=lambda *a: None, cancel=lambda *a: None, set_lease_expiry=lambda *a: None
        ),
        coordinator=None,
        orchestrators={},
        backend=SimpleNamespace(stop=lambda sid: None),
        hub=None,
        locks=SimpleNamespace(drop=lambda sid: None),
    )
    watcher = bl.BudgetLeaseEnforcer(
        d,
        bl.LeaseSettings(enabled=True, safe_boundary_wait=0),
        clock=lambda: now,
        candidates=lambda: {"A", "B"},
        is_speaking=lambda sid: True,
    )
    try:
        assert await asyncio.wait_for(watcher.sweep(), 1) == ["A", "B"]
        assert hard_stops == ["A", "B"]
        for sid in ("A", "B"):
            meta = await store.get(sid)
            assert meta["execution_contract"]["phase"] == "failed"
            assert meta[bl.UNSTAGED_KEY] and sid in watcher.tracked
        # The settling retry uses another complete, lock-owned detached attempt too.
        # Redis lock expiry is modeled by round2 OwnedStore for this second path.
    finally:
        release.set()
        await asyncio.gather(*database_tasks, return_exceptions=True)
