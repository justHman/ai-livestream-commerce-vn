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
from backend.application.usage_evidence import (
    UsageEvidence,
    UsageEvidenceSettings,
    UsageEvidenceUnavailable,
    UsageSender,
)
from backend.application.usage_evidence import envelope
from backend.application.usage_evidence.envelope import Draft
from backend.application.usage_evidence.outbox import StageBlocked, UsageOutbox

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


async def test_a_duplicate_trigger_restages_one_row_with_a_new_attempt_token(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [draft()])
    second = await outbox.stage(ident, [draft()])
    stored = await rows(pg, ident)
    assert len(stored) == 1  # one semantic row ...
    assert first[0].event_id == second[0].event_id == stored[0]["event_id"]
    assert first[0].token != second[0].token == stored[0]["stage_token"]  # ... one live attempt
    assert hashlib.sha256(bytes(stored[0]["body"])).hexdigest() == stored[0]["body_sha256"]
    await ready(outbox, first)  # the superseded attempt can never flip the row
    assert [r["status"] for r in await rows(pg, ident)] == ["staged"]
    await ready(outbox, second)
    assert [r["status"] for r in await rows(pg, ident)] == ["ready"]
    third = await outbox.stage(ident, [draft()])  # a final row is returned, never re-staged
    assert third[0].token is None and not third[0].created


async def test_semantic_identity_is_unique_per_identity_kind_and_interval(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(
        ident, [draft(), draft(occurred_at=datetime(2030, 1, 1, tzinfo=timezone.utc))]
    )
    stored = await rows(pg, ident)
    assert len(stored) == 1  # the live attempt (the later draft) owns the single row


async def test_usage_sequence_is_gap_free_and_increasing_with_concurrent_writers(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    staged = await asyncio.gather(
        *(
            outbox.stage(ident, [draft("unusable_started", "unusable", opening=str(i), seq=i)])
            for i in range(1, 21)
        )
    )
    assert all(s[0].usage_sequence is None for s in staged)  # numbered only when ready
    await asyncio.gather(*(outbox.mark_ready(pair(s)) for s in staged))
    sequences = [r["usage_sequence"] for r in await rows(pg, ident)]
    assert sequences == list(range(1, 21)) and await counter(pg, ident) == 20


async def test_two_identities_have_independent_sequences(pg):
    outbox, a, b = UsageOutbox(pg), new_identity(), new_identity()
    await ready(outbox, await outbox.stage(a, [draft()]))
    await ready(outbox, await outbox.stage(b, [draft()]))
    assert await counter(pg, a) == await counter(pg, b) == 1


async def test_the_number_and_final_bytes_are_fixed_at_ready_and_equal_a_direct_build(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    staged = await outbox.stage(ident, [draft()])
    before = (await rows(pg, ident))[0]
    assert json.loads(bytes(before["body"]))["payload"]["usage_sequence"] == 0
    await ready(outbox, staged)
    after = (await rows(pg, ident))[0]
    _, direct = envelope.build_body(
        ident,
        draft(),
        interval=envelope.interval_id(ident, "phase:selling", ""),
        usage_sequence=1,
        producer_id="runtime",
    )
    assert bytes(after["body"]) == direct and after["usage_sequence"] == 1
    assert hashlib.sha256(direct).hexdigest() == after["body_sha256"]
    assert after["event_id"] == before["event_id"]  # event_id never depended on the number
    await ready(outbox, staged)  # a second flip is a no-op: bytes are frozen
    assert bytes((await rows(pg, ident))[0]["body"]) == direct


async def test_a_discarded_earlier_row_leaves_no_gap_for_a_later_row(pg):
    """stage exec 100 (crash before save), later exec 5: the discard must not break the prefix."""
    outbox, ident = UsageOutbox(pg), new_identity()
    lost = await outbox.stage(ident, [draft("terminal", "terminal", seq=100)])
    later = await outbox.stage(ident, [draft(seq=5)])
    await outbox.release(pair(lost), delete=False)
    await ready(outbox, later)
    stored = {r["kind"]: r for r in await rows(pg, ident)}
    assert stored["phase_changed"]["usage_sequence"] == 1  # not 2
    assert stored["terminal"]["status"] == "discarded"
    assert [r["usage_sequence"] for r in await outbox.claim(50, 60)] == [1]  # deliverable


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


async def test_abort_removes_staged_rows_and_never_consumes_a_number(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    kept = await outbox.stage(ident, [draft()])
    gone = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)])
    await outbox.mark_ready(pair(kept))
    await outbox.release(pair(gone), delete=True)
    again = await outbox.stage(ident, [draft("unusable_started", "unusable", opening="1")])
    await ready(outbox, again)
    assert [r["usage_sequence"] for r in await rows(pg, ident)] == [1, 2]


async def sweeper(pg, store, locked=None, **env):
    settings = UsageEvidenceSettings.from_env(FAST | env)
    return UsageSender(UsageOutbox(pg), settings, session_store=store, session_lock=locked)


def pair(staged):
    return [(s.event_id, s.token) for s in staged if s.token]


def meta_at(ident, sequence, committed=(), version=None):
    return {
        "execution_contract": ExecutionState(**ident.model_dump(), sequence=sequence).model_dump(
            mode="json"
        ),
        "usage_evidence_commits": {
            "version": version if version is not None else len(committed),
            "tokens": {c: i + 1 for i, c in enumerate(committed)},
        },
    }


async def test_sweeper_converges_a_crash_before_save_to_discarded(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    await store.set(ident.runtime_session_id, meta_at(ident, 4))  # the save never happened
    await outbox.stage(ident, [draft(seq=5)])
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    stored = await rows(pg, ident)
    assert [r["status"] for r in stored] == ["discarded"] and stored[0]["usage_sequence"] is None
    assert await counter(pg, ident) is None  # never numbered: no fake gap


async def test_sweeper_converges_a_crash_after_save_to_ready(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    staged = await outbox.stage(ident, [draft(seq=5)])
    # the save happened (state AND the committed-fact proof), the ready flip did not
    await store.set(ident.runtime_session_id, meta_at(ident, 5, [staged[0].token]))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    stored = await rows(pg, ident)
    assert [r["status"] for r in stored] == ["ready"] and stored[0]["usage_sequence"] == 1


async def test_a_sequence_that_advanced_with_a_different_fact_does_not_publish_the_staged_one(pg):
    """Codex P1: unhealthy staged at exec seq 5 (crash before save); DIFFERENT healthy evidence
    then commits at seq 5. Sequence advancement does not prove which fact landed."""
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    unhealthy = await outbox.stage(
        ident, [draft("unusable_started", "unusable", opening="5", seq=5)]
    )
    healthy = await outbox.stage(ident, [draft("phase_changed", "phase:selling", seq=5)])
    # only the healthy fact was saved
    await store.set(ident.runtime_session_id, meta_at(ident, 5, [healthy[0].token]))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    by_kind = {r["kind"]: r["status"] for r in await rows(pg, ident)}
    assert by_kind == {"unusable_started": "discarded", "phase_changed": "ready"}
    assert unhealthy[0].event_id != healthy[0].event_id


async def test_sweeper_never_sends_or_drops_what_it_cannot_prove_but_parks_it(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(ident, [draft(seq=5)])
    await asyncio.sleep(0.05)
    await (await sweeper(pg, InMemorySessionStore())).sweep()  # session meta gone
    stored = (await rows(pg, ident))[0]
    assert stored["status"] == "staged" and stored["last_error"] == "session_missing"
    assert stored["attempts"] == 1


async def test_a_generation_change_parks_the_row_with_its_reason(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(ident, [draft(seq=5)])
    store = InMemorySessionStore()
    other = ExecutionIdentity(**(ident.model_dump() | {"generation": "g2"}))
    await store.set(ident.runtime_session_id, meta_at(other, 9))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    stored = (await rows(pg, ident))[0]
    assert stored["status"] == "staged" and stored["last_error"] == "generation_changed"


async def test_unresolvable_rows_cannot_starve_a_recoverable_one(pg):
    """Codex P2: 150 unresolvable rows + 1 recoverable; the oldest-100 window must move on."""
    outbox, store = UsageOutbox(pg), InMemorySessionStore()
    for _ in range(150):
        await outbox.stage(new_identity(), [draft(seq=5)])  # no session meta: unresolvable
    good = new_identity()
    staged = await outbox.stage(good, [draft(seq=5)])
    await store.set(good.runtime_session_id, meta_at(good, 5, [staged[0].token]))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    assert [r["status"] for r in await rows(pg, good)] == ["ready"]
    async with pg._require_pool().acquire() as conn:
        parked = await conn.fetchval(
            "SELECT count(*) FROM usage_evidence_outbox WHERE status = 'staged' AND attempts = 1"
        )
    assert parked == 150


async def test_an_unresolvable_row_is_dropped_with_an_audit_log_after_the_ttl(pg, caplog):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(ident, [draft(seq=5)])
    await asyncio.sleep(0.05)
    with caplog.at_level("ERROR"):
        await (
            await sweeper(pg, InMemorySessionStore(), USAGE_EVIDENCE_UNRESOLVED_TTL_SECONDS="0.01")
        ).sweep()
    assert [r["status"] for r in await rows(pg, ident)] == ["discarded"]
    assert "AUDIT dropped unresolvable" in caplog.text


async def test_retention_deletes_only_old_finished_rows_in_bounded_batches(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    staged = await outbox.stage(
        ident,
        [
            draft(),
            draft("terminal", "terminal", seq=9),
            draft("unusable_started", "unusable", opening="1"),
        ],
    )
    await ready(outbox, staged)
    async with pg._require_pool().acquire() as conn:
        await conn.execute(
            "UPDATE usage_evidence_outbox SET status = 'delivered', "
            "updated_at = NOW() - interval '30 days' WHERE kind IN ('phase_changed', 'terminal')"
        )
    assert await outbox.purge(14, 1) == 1  # batch limit honoured
    assert await outbox.purge(14, 10) == 1
    assert [r["status"] for r in await rows(pg, ident)] == ["ready"]  # undelivered is never purged


async def test_a_young_staged_row_is_left_alone_by_the_sweeper(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    await outbox.stage(ident, [draft(seq=5)])
    await (
        await sweeper(pg, InMemorySessionStore(), USAGE_EVIDENCE_SWEEP_AGE_SECONDS="600")
    ).sweep()
    assert [r["status"] for r in await rows(pg, ident)] == ["staged"]


async def test_a_discarded_row_is_revived_as_staged_and_numbered_only_when_ready(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [draft(seq=5)])
    await outbox.release(pair(first), delete=False)
    again = await outbox.stage(ident, [draft(seq=5)])
    assert again[0].event_id == first[0].event_id and again[0].usage_sequence is None
    assert [r["status"] for r in await rows(pg, ident)] == ["staged"]
    await ready(outbox, again)
    assert [r["usage_sequence"] for r in await rows(pg, ident)] == [1]


# -- delivery ------------------------------------------------------------------------------------


async def ready(outbox, staged):
    await outbox.mark_ready(pair(staged))


async def test_delivery_is_per_identity_in_usage_sequence_order(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [draft()])
    second = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)], version=2)
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
    second = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)], version=2)
    await ready(outbox, first + second)
    claimed = [r for r in await outbox.claim(50, 60) if r["usage_sequence"] == 1]
    await outbox.finish(first[0].event_id, claimed[0]["lease_token"], "rejected", http_status=400)
    assert [r for r in await outbox.claim(50, 60) if r["usage_sequence"] == 2]


async def test_an_earlier_unresolved_row_holds_back_numbering_until_it_resolves(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [draft()])  # stays staged: unresolved, no number
    second = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)], version=2)
    assert await outbox.mark_ready(pair(second)) == [] and await outbox.claim(50, 60) == []
    await outbox.release(pair(first), delete=False)  # it provably never committed
    assert await outbox.mark_ready(pair(second)) == [second[0].event_id]
    assert [r["usage_sequence"] for r in await outbox.claim(50, 60)] == [1]


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
    staged = await outbox.stage(ident, [draft()]) + await outbox.stage(
        ident, [draft("terminal", "terminal", seq=9)], version=2
    )
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
    second = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)], version=2)
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
    staged = await service._stage(ExecutionState(**ident.model_dump(), sequence=5), [draft()], {})
    await service.commit(staged)
    assert [r["status"] for r in await rows(pg, ident)] == ["ready"]
    other = new_identity()
    staged = await service._stage(ExecutionState(**other.model_dump(), sequence=5), [draft()], {})
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


# -- Codex round 2: attempt tokens, no proof window, locking, commit order -----------------------


def terminal(reason, seq):
    return draft("terminal", "terminal", seq=seq, reason_code=reason)


async def test_a_restaged_semantic_fact_is_never_authorized_by_the_old_attempts_token(pg):
    """P1: stage failed(seq2), lose the write, then commit ended(seq4): same semantic id."""
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    failed = await outbox.stage(ident, [terminal("execution_failed", 2)])
    ended = await outbox.stage(ident, [terminal("normal_end", 4)])
    assert failed[0].event_id == ended[0].event_id  # the semantic id is shared
    assert failed[0].token != ended[0].token
    await store.set(ident.runtime_session_id, meta_at(ident, 4, [ended[0].token]))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    (row,) = await rows(pg, ident)
    assert row["status"] == "ready"
    assert json.loads(bytes(row["body"]))["payload"]["reason_code"] == "normal_end"  # NOT failed
    # and the proof of the OLD attempt does not authorize the new bytes either
    other = new_identity()
    a = await outbox.stage(other, [terminal("execution_failed", 2)])
    b = await outbox.stage(other, [terminal("normal_end", 4)])
    store2 = InMemorySessionStore()
    await store2.set(other.runtime_session_id, meta_at(other, 2, [a[0].token]))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store2)).sweep()
    (row2,) = await rows(pg, other)
    assert row2["status"] == "discarded" and b[0].token == row2["stage_token"]


async def test_a_committed_unresolved_attempt_blocks_a_restage_instead_of_being_replaced(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    first = await outbox.stage(ident, [terminal("execution_failed", 2)])
    with pytest.raises(StageBlocked):
        await outbox.stage(ident, [terminal("normal_end", 4)], committed_tokens={first[0].token})
    (row,) = await rows(pg, ident)
    assert row["stage_token"] == first[0].token  # the committed payload is untouched


async def test_a_delayed_recovery_through_many_later_facts_still_resolves_by_token(pg):
    """P1: the proof must not evict. 600 later committed facts; the old row is still ready."""
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    old = await outbox.stage(ident, [draft("unusable_started", "unusable", opening="1", seq=1)])
    tokens = [old[0].token]
    for i in range(2, 602):
        tokens.append(f"later-{i}")  # later committed facts' tokens (rows already ready)
    await store.set(ident.runtime_session_id, meta_at(ident, 700, tokens, version=601))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    assert [r["status"] for r in await rows(pg, ident)] == ["ready"]


async def test_the_facade_prunes_tokens_only_for_rows_that_are_no_longer_staged(pg):
    ident = new_identity()
    outbox = UsageOutbox(pg)
    service = UsageEvidence(outbox, UsageEvidenceSettings.from_env(FAST))
    state = ExecutionState(**ident.model_dump(), sequence=5)
    meta: dict = {}
    one = await service._stage(state, [draft("unusable_started", "unusable", opening="1")], meta)
    UsageEvidence.stamp(meta, one)
    two = await service._stage(state, [draft("unusable_started", "unusable", opening="2")], meta)
    UsageEvidence.stamp(meta, two)
    assert set(meta["usage_evidence_commits"]["tokens"]) == {one[0].token, two[0].token}
    await service.commit(one)  # row one flips ready; row two stays staged (still unresolved)
    three = await service._stage(state, [draft("unusable_started", "unusable", opening="3")], meta)
    kept = set(meta["usage_evidence_commits"]["tokens"])
    assert two[0].token in kept  # unresolved: never dropped
    assert three and one[0].token not in kept  # ready: pruned


async def test_a_full_proof_fails_closed_instead_of_evicting(pg):
    service = UsageEvidence(UsageOutbox(pg), UsageEvidenceSettings.from_env(FAST))
    ident = new_identity()
    state = ExecutionState(**ident.model_dump(), sequence=5)
    meta = {"usage_evidence_commits": {"version": 9, "tokens": {f"t{i}": i for i in range(512)}}}
    # none of the 512 tokens has a staged row, so they prune away: staging works
    await service._stage(state, [draft()], meta)
    full = new_identity()
    outbox = UsageOutbox(pg)
    live = {}
    for i in range(512):
        st = await outbox.stage(
            full, [draft("unusable_started", "unusable", opening=str(i), seq=i)], version=i + 1
        )
        live[st[0].token] = i + 1
    with pytest.raises(UsageEvidenceUnavailable):
        await service._stage(
            ExecutionState(**full.model_dump(), sequence=5),
            [draft("unusable_started", "unusable", opening="x")],
            {"usage_evidence_commits": {"version": 512, "tokens": live}},
        )


async def test_the_sweeper_waits_for_the_session_lock_and_never_discards_an_inflight_retry(pg):
    """P1: the unlocked sweeper read Redis before the retry saved, then discarded the row."""
    from backend.api.v1.execution import _locked

    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    sid = ident.runtime_session_id
    await store.set(sid, meta_at(ident, 4))  # not yet saved
    old = await outbox.stage(ident, [draft(seq=5)])
    await asyncio.sleep(0.05)
    sweep = await sweeper(pg, store, locked=lambda session: _locked(store, session))
    async with _locked(store, sid):  # the retry holds the session lock: stage, save, commit
        retry = await outbox.stage(ident, [draft(seq=5)])
        await asyncio.sleep(0.05)
        task = asyncio.create_task(
            sweep.sweep()
        )  # the sweeper starts while the retry is mid-flight
        await asyncio.sleep(0.1)
        assert not task.done()  # it is blocked on the lock, it did not read stale Redis
        meta = meta_at(ident, 5, [retry[0].token])
        await store.set(sid, meta)  # the save lands
        await outbox.mark_ready(pair(retry))
    await task
    (row,) = await rows(pg, ident)
    assert row["status"] == "ready" and row["stage_token"] == retry[0].token
    assert old[0].token != retry[0].token


async def test_the_sweeper_skips_a_row_that_was_restaged_after_it_selected_it(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    await store.set(ident.runtime_session_id, meta_at(ident, 4))
    old = await outbox.stage(ident, [draft(seq=5)])
    await asyncio.sleep(0.05)
    sw = await sweeper(pg, store)
    stale = await outbox.stale_staged(0.01)
    new = await outbox.stage(ident, [draft(seq=5)])  # re-staged after selection
    assert await sw._resolve(stale) == 0  # compare-and-set on the token: untouched
    (row,) = await rows(pg, ident)
    assert row["status"] == "staged" and row["stage_token"] == new[0].token
    assert old[0].token != new[0].token


async def test_mark_ready_revives_a_discarded_row_when_the_proof_shows_commit(pg, caplog):
    outbox, ident = UsageOutbox(pg), new_identity()
    st = await outbox.stage(ident, [draft(seq=5)])
    await outbox.release(pair(st), delete=False)
    with caplog.at_level("ERROR"):
        flipped = await outbox.mark_ready(pair(st))
    assert flipped == [st[0].event_id] and "REVIVED" in caplog.text
    assert [r["status"] for r in await rows(pg, ident)] == ["ready"]


async def test_numbering_follows_commit_order_not_ready_flip_order(pg):
    """P2: START committed (flip deferred), END committed and flipped, START recovered."""
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    start = await outbox.stage(
        ident, [draft("unusable_started", "unusable", opening="5", seq=5)], version=1
    )
    end = await outbox.stage(
        ident, [draft("unusable_ended", "unusable", opening=None, boundary="end", seq=6)], version=2
    )
    assert end  # END's request path tries to flip first: it must wait for the earlier START
    assert await outbox.mark_ready(pair(end)) == []
    await store.set(ident.runtime_session_id, meta_at(ident, 6, [start[0].token, end[0].token]))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    by_kind = {r["kind"]: r["usage_sequence"] for r in await rows(pg, ident)}
    assert by_kind == {"unusable_started": 1, "unusable_ended": 2}


async def test_a_lower_version_row_that_never_committed_is_discarded_and_unblocks_the_next(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    store = InMemorySessionStore()
    lost = await outbox.stage(ident, [draft("terminal", "terminal", seq=9)], version=1)
    later = await outbox.stage(ident, [draft(seq=5)], version=2)
    assert await outbox.mark_ready(pair(later)) == []  # waits behind the unresolved earlier row
    await store.set(ident.runtime_session_id, meta_at(ident, 5, [later[0].token]))
    await asyncio.sleep(0.05)
    await (await sweeper(pg, store)).sweep()
    by_kind = {r["kind"]: (r["status"], r["usage_sequence"]) for r in await rows(pg, ident)}
    assert by_kind == {"terminal": ("discarded", None), "phase_changed": ("ready", 1)}
    assert lost


async def test_retry_state_is_persisted_the_attempt_counter_survives_a_sender_restart(pg):
    outbox, ident = UsageOutbox(pg), new_identity()
    st = await outbox.stage(ident, [draft()])
    await ready(outbox, st)
    receiver = Receiver()
    receiver.status_override = 503
    await drain(sender_for(pg, receiver), receiver, 4)
    attempts_before = (await rows(pg, ident))[0]["attempts"]
    assert attempts_before >= 2
    again = await outbox.claim(50, 0.01)  # a new process continues from the stored counter
    assert again and again[0]["attempts"] > attempts_before
    stored = await rows(pg, ident)
    assert stored[0]["last_status"] == 503 and stored[0]["last_error"] == "http_503"
