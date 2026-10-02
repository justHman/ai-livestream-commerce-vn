"""P0-FB-017 usage-evidence outbox against a real, disposable PostgreSQL.

Set USAGE_EVIDENCE_TEST_DATABASE_URL to a loopback database (never a shared one):
    postgresql://user@127.0.0.1:PORT/livento_017_runtime_test
Run with:  pytest tests/integration/test_usage_evidence_pg.py -o addopts=""
Skipped (and so NOT a pass) when the variable is absent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest

from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.db.postgres_store import PostgresRuntimeStore, schema_sql
from backend.application.execution_contract import ExecutionIdentity, ExecutionState
from backend.application.usage_evidence import UsageEvidence, UsageEvidenceSettings, UsageSender
from backend.application.usage_evidence import envelope
from backend.application.usage_evidence.envelope import Draft
from backend.application.usage_evidence.outbox import UsageOutbox

URL = os.environ.get("USAGE_EVIDENCE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL, reason="set USAGE_EVIDENCE_TEST_DATABASE_URL for the P0-FB-017 outbox suite"
)
NOW = datetime(2026, 10, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
SECRET = "pg-suite-secret-not-a-credential"
RECEIVER_URL = "http://127.0.0.1:9/webhooks/ai/events"
FAST = {
    "USAGE_EVIDENCE_ENABLED": "1",
    "USAGE_EVIDENCE_SECRET": SECRET,
    "USAGE_EVIDENCE_URL": RECEIVER_URL,
    "USAGE_EVIDENCE_BACKOFF_BASE_SECONDS": "0.01",
    "USAGE_EVIDENCE_BACKOFF_CAP_SECONDS": "0.02",
    "USAGE_EVIDENCE_SWEEP_AGE_SECONDS": "0.01",
}


def test_the_test_database_is_loopback_and_named_for_this_suite():
    parsed = urlparse(URL)
    assert parsed.hostname == "127.0.0.1" and parsed.path.startswith("/livento_017_")


def test_the_always_applied_runtime_schema_does_not_contain_the_usage_tables():
    assert "usage_evidence" not in schema_sql()


@pytest.fixture
async def pg():
    store = PostgresRuntimeStore(URL)
    await store.connect()
    await store.apply_usage_evidence_schema()
    await store.apply_usage_evidence_schema()  # idempotent
    async with store._require_pool().acquire() as conn:  # disposable database: isolate each test
        await conn.execute("TRUNCATE usage_evidence_outbox, usage_evidence_sequence")
    yield store
    await store.close()


def new_identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        tenant_id=str(uuid.uuid4()),
        business_session_id="biz-" + uuid.uuid4().hex,
        runtime_session_id="rt-" + uuid.uuid4().hex,
        generation="g1",
    )


def draft(kind="phase_changed", interval_kind="phase:selling", opening="", seq=5, **kw) -> Draft:
    return Draft(
        kind,
        interval_kind,
        kw.pop("boundary", "start"),
        opening,
        phase="selling",
        occurred_at=kw.pop("occurred_at", NOW),
        applied_sequence=seq,
        execution_sequence=seq,
        **kw,
    )


async def rows(pg, identity, *, order="usage_sequence"):
    async with pg._require_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM usage_evidence_outbox WHERE tenant_id = $1 AND runtime_session_id = $2 "
            f"ORDER BY {order} NULLS LAST, created_at",
            identity.tenant_id,
            identity.runtime_session_id,
        )


async def counter(pg, identity) -> int:
    async with pg._require_pool().acquire() as conn:
        return await conn.fetchval(
            "SELECT last_usage_sequence FROM usage_evidence_sequence WHERE tenant_id = $1 "
            "AND runtime_session_id = $2",
            identity.tenant_id,
            identity.runtime_session_id,
        )


# -- sequence, idempotency, identity ---------------------------------------------------------


async def test_a_duplicate_trigger_returns_the_existing_row_with_identical_bytes(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [draft()])
    second = await outbox.stage(ident, [draft()])
    stored = await rows(pg, ident)
    assert len(stored) == 1
    assert first[0].created and not second[0].created
    assert first[0].event_id == second[0].event_id == stored[0]["event_id"]
    assert hashlib.sha256(bytes(stored[0]["body"])).hexdigest() == stored[0]["body_sha256"]


async def test_semantic_identity_is_unique_per_identity_kind_and_interval(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(
        ident, [draft(), draft(occurred_at=datetime(2030, 1, 1, tzinfo=timezone.utc))]
    )
    stored = await rows(pg, ident)
    assert len(stored) == 1 and stored[0]["occurred_at"] == NOW  # the first fact time wins


async def test_usage_sequence_is_gap_free_and_increasing_with_concurrent_writers(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await asyncio.gather(
        *(
            outbox.stage(ident, [draft("unusable_started", "unusable", opening=str(i), seq=i)])
            for i in range(1, 21)
        )
    )
    sequences = [r["usage_sequence"] for r in await rows(pg, ident)]
    assert sequences == list(range(1, 21)) and await counter(pg, ident) == 20


async def test_two_identities_have_independent_sequences(pg):
    outbox, a, b = UsageOutbox(pg), new_identity(), new_identity()
    await outbox.stage(a, [draft()])
    await outbox.stage(b, [draft()])
    assert await counter(pg, a) == await counter(pg, b) == 1


async def test_an_undeliverable_identity_is_rejected_at_insert_and_by_the_database(pg):
    outbox = UsageOutbox(pg)
    bad = ExecutionIdentity(**(new_identity().model_dump() | {"tenant_id": "not-a-uuid"}))
    with pytest.raises(envelope.InvalidIdentity):
        await outbox.stage(bad, [draft()])
    async with pg._require_pool().acquire() as conn:
        with pytest.raises(Exception) as caught:
            await conn.execute(
                "INSERT INTO usage_evidence_outbox (event_id, event_type, tenant_id, "
                "business_session_id, runtime_session_id, generation, kind, interval_id, "
                "occurred_at, body, body_sha256) VALUES ('x','ai.usage.reported','nope','b','r',"
                f"'g','k','i',NOW(),'\\x00','{'0' * 64}')"
            )
    assert "check" in str(caught.value).lower()


async def test_the_unusable_end_closes_the_stored_start_interval_and_never_invents_one(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    end = draft("unusable_ended", "unusable", opening=None, boundary="end", seq=8)
    assert await outbox.stage(ident, [end]) == []  # nothing stored to close
    started = await outbox.stage(ident, [draft("unusable_started", "unusable", opening="6", seq=6)])
    closed = await outbox.stage(ident, [end])
    stored = {r["kind"]: json.loads(bytes(r["body"])) for r in await rows(pg, ident)}
    assert closed and started
    assert (
        stored["unusable_started"]["payload"]["interval"]["interval_id"]
        == stored["unusable_ended"]["payload"]["interval"]["interval_id"]
    )


# -- the commit gap ---------------------------------------------------------------------------


async def test_abort_removes_staged_rows_and_returns_their_numbers(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    kept = await outbox.stage(ident, [draft()])
    gone = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)])
    await outbox.mark_ready([kept[0].event_id])
    await outbox.release([gone[0].event_id], delete=True)
    again = await outbox.stage(ident, [draft("unusable_started", "unusable", opening="1")])
    assert again[0].usage_sequence == 2 and [
        r["usage_sequence"] for r in await rows(pg, ident)
    ] == [1, 2]


async def sweeper(pg, store, **env):
    settings = UsageEvidenceSettings.from_env(FAST | env)
    return UsageSender(UsageOutbox(pg), settings, session_store=store)


async def test_sweeper_converges_a_crash_before_save_to_discarded(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    await store.set(  # the save never happened: the meta is still at sequence 4
        ident.runtime_session_id,
        {
            "execution_contract": ExecutionState(**ident.model_dump(), sequence=4).model_dump(
                mode="json"
            )
        },
    )
    await outbox.stage(ident, [draft(seq=5)])
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    stored = await rows(pg, ident)
    assert [r["status"] for r in stored] == ["discarded"] and stored[0]["usage_sequence"] is None
    assert await counter(pg, ident) == 0  # the number was released: no fake gap


async def test_sweeper_converges_a_crash_after_save_to_ready(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    await store.set(  # the save happened, the ready flip did not
        ident.runtime_session_id,
        {
            "execution_contract": ExecutionState(**ident.model_dump(), sequence=5).model_dump(
                mode="json"
            )
        },
    )
    await outbox.stage(ident, [draft(seq=5)])
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    assert [r["status"] for r in await rows(pg, ident)] == ["ready"]


async def test_sweeper_never_sends_or_drops_what_it_cannot_prove(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(ident, [draft(seq=5)])
    await asyncio.sleep(0.05)
    await (await sweeper(pg, InMemorySessionStore())).sweep()  # session meta unreadable/gone
    assert [r["status"] for r in await rows(pg, ident)] == ["staged"]


async def test_a_young_staged_row_is_left_alone_by_the_sweeper(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(ident, [draft(seq=5)])
    await (
        await sweeper(pg, InMemorySessionStore(), USAGE_EVIDENCE_SWEEP_AGE_SECONDS="600")
    ).sweep()
    assert [r["status"] for r in await rows(pg, ident)] == ["staged"]


async def test_a_discarded_row_is_revived_under_a_fresh_number_when_the_fact_is_retried(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [draft(seq=5)])
    await outbox.release([first[0].event_id], delete=False)
    again = await outbox.stage(ident, [draft(seq=5)])
    assert again[0].event_id == first[0].event_id and again[0].usage_sequence == 1
    assert [r["status"] for r in await rows(pg, ident)] == ["staged"]


# -- delivery ------------------------------------------------------------------------------------


async def ready(outbox, staged):
    await outbox.mark_ready([s.event_id for s in staged])


async def test_delivery_is_per_identity_in_usage_sequence_order(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [draft()])
    second = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)])
    await ready(outbox, first + second)
    claimed = [r for r in await outbox.claim(50, 60) if r["usage_sequence"] in (1, 2)]
    assert [r["usage_sequence"] for r in claimed] == [1]  # row 2 waits for row 1
    assert await outbox.finish(
        first[0].event_id, claimed[0]["lease_token"], "delivered", http_status=202
    )
    again = [r for r in await outbox.claim(50, 60) if r["usage_sequence"] == 2]
    assert len(again) == 1


async def test_a_permanently_rejected_row_does_not_block_a_later_row(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [draft()])
    second = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)])
    await ready(outbox, first + second)
    claimed = [r for r in await outbox.claim(50, 60) if r["usage_sequence"] == 1]
    await outbox.finish(first[0].event_id, claimed[0]["lease_token"], "rejected", http_status=400)
    assert [r for r in await outbox.claim(50, 60) if r["usage_sequence"] == 2]


async def test_a_staged_earlier_row_holds_back_later_ready_rows(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(ident, [draft()])  # stays staged
    second = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)])
    await ready(outbox, second)
    assert [r for r in await outbox.claim(50, 60) if r["usage_sequence"] == 2] == []


async def test_a_stale_lease_cannot_overwrite_a_newer_outcome(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    staged = await outbox.stage(ident, [draft()])
    await ready(outbox, staged)
    old = (await outbox.claim(50, 0.01))[0]
    await asyncio.sleep(0.05)
    new = [r for r in await outbox.claim(50, 60) if r["event_id"] == staged[0].event_id][0]
    assert not await outbox.finish(
        staged[0].event_id, old["lease_token"], "delivered", http_status=202
    )
    assert await outbox.finish(staged[0].event_id, new["lease_token"], "delivered", http_status=202)
    assert not await outbox.finish(
        staged[0].event_id, new["lease_token"], "ready"
    )  # never resurrected


class Receiver:
    """The API receiver's observable rules: HMAC, window, header==body id, 409 on new bytes."""

    def __init__(self):
        self.seen: dict[str, str] = {}
        self.calls: list[tuple[bytes, dict]] = []
        self.down = False
        self.status_override: int | None = None
        self.clock = lambda: datetime.now(timezone.utc).timestamp()

    async def __call__(self, url, body, headers):
        self.calls.append((body, dict(headers)))
        if self.down:
            raise ConnectionRefusedError("outage")
        if self.status_override is not None:
            return self.status_override
        want = hmac.new(
            SECRET.encode(), headers["X-AI-Timestamp"].encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        if (
            want != headers["X-AI-Signature"]
            or abs(self.clock() - int(headers["X-AI-Timestamp"])) > 300
        ):
            return 401
        doc = json.loads(body)
        digest = hashlib.sha256(body).hexdigest()
        if self.seen.get(doc["event_id"], digest) != digest:
            return 409
        self.seen[doc["event_id"]] = digest
        return 202


def sender_for(pg, receiver, store=None):
    return UsageSender(
        UsageOutbox(pg),
        UsageEvidenceSettings.from_env(FAST),
        session_store=store,
        post=receiver,
    )


async def drain(sender, receiver, rounds=40):
    for _ in range(rounds):
        await sender.deliver_due()
        await asyncio.sleep(0.03)


async def test_an_outage_never_drops_rows_and_recovery_delivers_everything_in_order(pg):
    outbox, ident, receiver = UsageOutbox(pg), new_identity(), Receiver()
    staged = await outbox.stage(ident, [draft(), draft("terminal", "terminal", seq=9)])
    await ready(outbox, staged)
    receiver.down = True
    sender = sender_for(pg, receiver)
    await drain(sender, receiver, 5)
    assert {r["status"] for r in await rows(pg, ident)} == {"ready"}
    backlog = await outbox.backlog()
    assert backlog["count"] >= 2 and backlog["oldest_age_seconds"] >= 0
    receiver.down = False
    await drain(sender, receiver)
    stored = await rows(pg, ident)
    assert {r["status"] for r in stored} == {"delivered"}
    ok = [json.loads(b)["payload"]["usage_sequence"] for b, _ in receiver.calls][-2:]
    assert ok == [1, 2]


async def test_retries_after_a_sender_restart_carry_identical_bytes_and_the_receiver_dedups(pg):
    outbox, ident, receiver = UsageOutbox(pg), new_identity(), Receiver()
    staged = await outbox.stage(ident, [draft()])
    await ready(outbox, staged)
    stored_body = bytes((await rows(pg, ident))[0]["body"])
    receiver.status_override = 503
    await drain(sender_for(pg, receiver), receiver, 3)
    receiver.status_override = None
    await drain(sender_for(pg, receiver), receiver)  # a "restarted" sender: new instance
    bodies = {b for b, _ in receiver.calls}
    assert bodies == {stored_body} and len(receiver.seen) == 1
    assert [r["status"] for r in await rows(pg, ident)] == ["delivered"]


async def test_a_replay_of_a_delivered_body_is_a_duplicate_and_creates_no_second_row(pg):
    outbox, ident, receiver = UsageOutbox(pg), new_identity(), Receiver()
    staged = await outbox.stage(ident, [draft()])
    await ready(outbox, staged)
    await drain(sender_for(pg, receiver), receiver, 3)
    body, headers = receiver.calls[0]
    assert await receiver("u", body, headers) == 202
    assert len(await rows(pg, ident)) == 1 and len(receiver.seen) == 1


async def test_a_conflict_is_marked_permanent_and_never_resent_while_later_rows_still_go(pg):
    outbox, ident, receiver = UsageOutbox(pg), new_identity(), Receiver()
    first = await outbox.stage(ident, [draft()])
    second = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)])
    await ready(outbox, first + second)
    first_body = bytes((await rows(pg, ident))[0]["body"])
    receiver.seen[first[0].event_id] = "0" * 64  # the receiver holds different bytes for this id
    sender = sender_for(pg, receiver)
    await drain(sender, receiver)
    stored = await rows(pg, ident)
    assert [r["status"] for r in stored] == ["conflict", "delivered"]
    assert [b for b, _ in receiver.calls].count(first_body) == 1  # sent once, never again
    assert stored[0]["last_status"] == 409 and sender.permanent_failures == 1


async def test_a_delayed_terminal_report_keeps_its_fact_time_and_its_sequence(pg):
    outbox, ident, receiver = UsageOutbox(pg), new_identity(), Receiver()
    long_ago = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    staged = await outbox.stage(
        ident, [draft("terminal", "terminal", "ended", seq=9, occurred_at=long_ago)]
    )
    await ready(outbox, staged)
    await asyncio.sleep(0.05)
    await drain(sender_for(pg, receiver), receiver, 3)
    doc = json.loads(receiver.calls[-1][0])
    assert doc["timestamp"] == "2026-01-01T12:00:00.000000Z"
    assert doc["payload"]["usage_sequence"] == 1 and doc["payload"]["kind"] == "terminal"
    assert [r["status"] for r in await rows(pg, ident)] == ["delivered"]


async def test_each_attempt_is_re_signed_with_a_fresh_timestamp_over_the_same_bytes(pg):
    outbox, ident, receiver = UsageOutbox(pg), new_identity(), Receiver()
    staged = await outbox.stage(ident, [draft()])
    await ready(outbox, staged)
    receiver.status_override = 401
    clock = iter([1000.0, 2000.0, 3000.0, 4000.0, 5000.0, 6000.0])
    sender = UsageSender(
        UsageOutbox(pg),
        UsageEvidenceSettings.from_env(FAST),
        post=receiver,
        clock=lambda: next(clock),
    )
    await drain(sender, receiver, 6)
    assert len({b for b, _ in receiver.calls}) == 1
    assert len({h["X-AI-Timestamp"] for _, h in receiver.calls}) == len(receiver.calls) >= 2


async def test_vendor_cogs_rides_ai_usage_reported_unordered_and_never_blocks_lifecycle(pg):
    outbox, ident, receiver = UsageOutbox(pg), new_identity(), Receiver()
    settings = UsageEvidenceSettings.from_env(FAST)
    service = UsageEvidence(outbox, settings)
    service.record_cogs(ident, model_id="m", input_tokens=10, output_tokens=20, sample_id="s1")
    staged = await outbox.stage(ident, [draft()])  # stays staged: blocks lifecycle rows only
    sender = UsageSender(outbox, settings, cogs=service.cogs, post=receiver)
    await sender.drain_cogs()
    await drain(sender, receiver, 3)
    docs = [json.loads(b) for b, _ in receiver.calls]
    assert [d["event_type"] for d in docs] == ["ai.usage.reported"]
    assert docs[0]["payload"]["input_tokens"] == 10 and "interval" not in docs[0]["payload"]
    assert staged and [r["status"] for r in await rows(pg, ident, order="created_at")] == [
        "staged",
        "delivered",
    ]


async def test_the_facade_stages_commits_and_aborts_only_through_the_outbox(pg):
    ident = new_identity()
    service = UsageEvidence(UsageOutbox(pg), UsageEvidenceSettings.from_env(FAST))
    staged = await service._stage(ExecutionState(**ident.model_dump(), sequence=5), [draft()])
    await service.commit(staged)
    assert [r["status"] for r in await rows(pg, ident)] == ["ready"]
    other = new_identity()
    staged = await service._stage(ExecutionState(**other.model_dump(), sequence=5), [draft()])
    await service.abort(staged)
    assert await rows(pg, other) == []


# -- wiring: lifespan fail-closed, and the real endpoint -> outbox path -----------------------


async def test_lifespan_advertises_only_when_store_url_and_secret_exist_and_stops_cleanly(
    pg, monkeypatch
):
    from types import SimpleNamespace

    from backend.application import execution_contract as ec
    from backend.bootstrap.lifespan import _start_usage_evidence, _stop_usage_evidence

    for key, value in FAST.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("USAGE_EVIDENCE_GATE_HOLD", "1")
    container = SimpleNamespace(pg_store=pg, store=InMemorySessionStore())
    await _start_usage_evidence(container)
    try:
        assert container.usage_evidence is not None
        assert {"usage.evidence.v1", "usage.evidence.hold"} <= set(ec.available_capabilities())
        assert "usage.evidence.first_broadcast" not in ec.available_capabilities()
    finally:
        await _stop_usage_evidence(container)
    assert "usage.evidence.v1" not in ec.available_capabilities()
    for missing in ("USAGE_EVIDENCE_SECRET", "USAGE_EVIDENCE_URL"):
        monkeypatch.delenv(missing)
        bare = SimpleNamespace(pg_store=pg, store=InMemorySessionStore())
        await _start_usage_evidence(bare)
        assert getattr(bare, "usage_evidence", None) is None
        assert "usage.evidence.v1" not in ec.available_capabilities()
        monkeypatch.setenv(missing, FAST[missing])
    nopg = SimpleNamespace(pg_store=None, store=InMemorySessionStore())
    await _start_usage_evidence(nopg)
    assert getattr(nopg, "usage_evidence", None) is None


async def test_the_real_evidence_endpoint_stages_then_readies_the_row_and_rejects_emit_nothing(pg):
    from fastapi import HTTPException, Request
    from types import SimpleNamespace

    from backend.api.v1.execution import record_execution_evidence
    from backend.application.execution_contract import Evidence

    ident = new_identity()
    store = InMemorySessionStore()
    await store.set(
        ident.runtime_session_id,
        {"execution_contract": ExecutionState(**ident.model_dump()).model_dump(mode="json")},
    )
    service = UsageEvidence(UsageOutbox(pg), UsageEvidenceSettings.from_env(FAST))
    container = SimpleNamespace(store=store, usage_evidence=service)
    request = Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(container=container))}
    )

    def evidence(seq, kind, phase, **kw):
        return Evidence(
            **ident.model_dump(), sequence=seq, kind=kind, phase=phase, occurred_at=NOW, **kw
        )

    sid = ident.runtime_session_id
    await record_execution_evidence(sid, evidence(1, "runtime_ready", "ready"), request, None)
    assert await rows(pg, ident) == []  # runtime_ready is not a usage fact
    with pytest.raises(HTTPException):  # rejected: preparing is gone, sequence is stale
        await record_execution_evidence(sid, evidence(1, "phase_changed", "ending"), request, None)
    await record_execution_evidence(sid, evidence(2, "phase_changed", "ending"), request, None)
    await record_execution_evidence(
        sid, evidence(3, "terminal", "ended", reason_code="normal_end"), request, None
    )
    stored = await rows(pg, ident)
    assert [(r["kind"], r["status"], r["usage_sequence"]) for r in stored] == [
        ("phase_changed", "ready", 1),
        ("terminal", "ready", 2),
    ]
