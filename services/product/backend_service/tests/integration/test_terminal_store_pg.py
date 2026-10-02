"""P0-FB-019 durable terminal record + outbox against a real, disposable PostgreSQL.

Set TERMINAL_TEST_DATABASE_URL to a loopback database (never a shared one):
    postgresql://user:pw@127.0.0.1:PORT/livento_019_runtime_test
Run with:  pytest tests/integration/test_terminal_store_pg.py -o addopts=""
Skipped (and so NOT a pass) when the variable is absent.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore, schema_sql
from backend.application.execution_contract import (
    Cleanup,
    CleanupRefs,
    ExecutionIdentity,
    TerminalRecord,
)
from backend.application.terminal_outcomes import TerminalOutbox, TerminalSettings

URL = os.environ.get("TERMINAL_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL, reason="set TERMINAL_TEST_DATABASE_URL for the P0-FB-019 store suite"
)
NOW = datetime(2026, 9, 30, 1, 2, 3, 456789, tzinfo=timezone.utc)


def test_the_test_database_is_loopback_and_named_for_this_suite():
    parsed = urlparse(URL)
    assert parsed.hostname == "127.0.0.1" and parsed.path.startswith("/livento_019_")


async def open_store() -> PostgresRuntimeStore:
    store = PostgresRuntimeStore(URL)
    await store.connect()
    await store.apply_terminal_schema()
    return store


def record(**changes) -> TerminalRecord:
    identity = ExecutionIdentity(
        tenant_id="tenant-" + uuid.uuid4().hex,
        business_session_id="business-" + uuid.uuid4().hex,
        runtime_session_id="rt-" + uuid.uuid4().hex,
        generation="g1",
    )
    base: dict[str, Any] = dict(
        identity=identity,
        terminal_phase="ended",
        reason_code="normal_end",
        business_outcome="ENDED",
        source="runtime",
        terminal_sequence=9,
        first_ai_broadcast=True,
        terminal_at=NOW,
        recorded_at=NOW,
        cleanup=Cleanup(status="succeeded", attempts=1),
    )
    return TerminalRecord.model_validate(base | changes).sealed()


async def row_count(store, table: str, rid: str) -> int:
    async with store._require_pool().acquire() as conn:
        return await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE terminal_record_id = $1", rid
        )


def test_the_always_applied_runtime_schema_does_not_contain_the_terminal_tables():
    assert "terminal_" not in schema_sql()


async def test_persist_once_and_a_duplicate_returns_the_first_record_unchanged():
    store = await open_store()
    try:
        first = record()
        stored, created, conflict = await store.persist_terminal(first)
        assert created and not conflict and stored.record_hash == first.record_hash
        later = first.model_copy(update={"recorded_at": NOW.replace(second=59)}).sealed()
        # A different core under the same id (here only the timestamp differs) is a conflict.
        again, created, conflict = await store.persist_terminal(later)
        assert not created and again.record_hash == first.record_hash
        assert conflict == "conflicting_terminal"
        same, created, conflict = await store.persist_terminal(first)
        assert (created, conflict, same.record_hash) == (False, "", first.record_hash)
        assert await row_count(store, "terminal_records", first.terminal_record_id) == 1
        assert (
            await row_count(store, "terminal_outbox", first.terminal_record_id) == 2
        )  # primary + late evidence
    finally:
        await store.close()


async def test_a_failure_after_ended_never_rewrites_the_stored_terminal():
    store = await open_store()
    try:
        ended = record()
        failed = record(
            identity=ended.identity,
            terminal_phase="failed",
            reason_code="execution_failed",
            failure_class="media_failed",
            business_outcome="FAILED",
        )
        await store.persist_terminal(ended)
        stored, created, conflict = await store.persist_terminal(failed)
        assert not created and (stored.terminal_phase, stored.business_outcome) == (
            "ended",
            "ENDED",
        )
        assert conflict == "late_failure"
        async with store._require_pool().acquire() as conn:
            kept = await conn.fetchrow(
                "SELECT audit FROM terminal_conflicts WHERE terminal_record_id = $1 AND incoming_hash = $2",
                failed.terminal_record_id,
                failed.record_hash,
            )
            queued = await conn.fetchrow(
                "SELECT kind, status FROM terminal_outbox WHERE terminal_record_id = $1 AND record_hash = $2",
                failed.terminal_record_id,
                failed.record_hash,
            )
        assert kept["audit"] == "late_failure"
        assert (queued["kind"], queued["status"]) == ("late_evidence", "pending")
    finally:
        await store.close()


async def test_concurrent_persists_create_exactly_one_record():
    store = await open_store()
    try:
        first = record()
        results = await asyncio.gather(*(store.persist_terminal(first) for _ in range(8)))
        assert sum(1 for r in results if r.created) == 1
        assert await row_count(store, "terminal_outbox", first.terminal_record_id) == 1
    finally:
        await store.close()


async def test_crash_after_persist_before_callback_redelivers_after_restart():
    store = await open_store()
    rec = record()
    await store.persist_terminal(rec)
    await store.close()  # the process dies here: nothing was sent

    restarted = await open_store()
    try:
        sent = []

        async def post(url, body, headers):
            sent.append(body)
            return 201, None

        outbox = TerminalOutbox(
            restarted, TerminalSettings(True, "https://api.example/r", "s"), post=post
        )
        processed = 0
        for _ in range(3):  # other tests share the database; drain until ours is delivered
            processed += await outbox.deliver_due()
            if any(rec.terminal_record_id.encode() in body for body in sent):
                break
        assert any(rec.terminal_record_id.encode() in body for body in sent)
        async with restarted._require_pool().acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT status FROM terminal_outbox WHERE terminal_record_id = $1",
                    rec.terminal_record_id,
                )
                == "delivered"
            )
    finally:
        await restarted.close()


async def test_crash_after_api_reconcile_before_ack_redelivers_identical_bytes_as_a_noop():
    store = await open_store()
    try:
        rec = record()
        await store.persist_terminal(rec)
        bodies, answers = [], iter([TimeoutError("ack lost"), (200, None)])

        async def post(url, body, headers):
            if rec.terminal_record_id.encode() in body:
                bodies.append(body)
                answer = next(answers)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return 201, None

        outbox = TerminalOutbox(
            store, TerminalSettings(True, "https://api.example/r", "s"), post=post, rng=lambda: 0.0
        )
        await (
            outbox.deliver_due()
        )  # the API applied it, the ack was lost: row stays pending with backoff
        async with store._require_pool().acquire() as conn:
            await conn.execute(
                "UPDATE terminal_outbox SET next_attempt_at = NOW() WHERE terminal_record_id = $1",
                rec.terminal_record_id,
            )
        await outbox.deliver_due()
        assert len(bodies) == 2 and bodies[0] == bodies[1]
        async with store._require_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, attempts FROM terminal_outbox WHERE terminal_record_id = $1",
                rec.terminal_record_id,
            )
        assert (row["status"], row["attempts"]) == ("delivered", 2)
    finally:
        await store.close()


async def test_outbox_claims_are_leased_so_two_senders_do_not_double_deliver():
    store = await open_store()
    try:
        rec = record()
        await store.persist_terminal(rec)
        a, b = await asyncio.gather(
            store.claim_terminal_outbox(1000, 60.0), store.claim_terminal_outbox(1000, 60.0)
        )
        owners = [r for r in (*a, *b) if r["terminal_record_id"] == rec.terminal_record_id]
        assert len(owners) == 1
        expired = await store.claim_terminal_outbox(1000, 0.0)  # still leased for 60 s: not due
        assert all(r["terminal_record_id"] != rec.terminal_record_id for r in expired)
    finally:
        await store.close()


async def test_cleanup_retries_update_only_cleanup_and_are_redelivered():
    store = await open_store()
    try:
        rec = record(cleanup=Cleanup(status="pending", attempts=0))
        await store.persist_terminal(rec)
        retry = Cleanup(
            status="retrying", attempts=2, last_error_class="egress", refs=CleanupRefs(media="m1")
        )
        assert await store.update_terminal_cleanup(rec.terminal_record_id, retry)
        assert not await store.update_terminal_cleanup(
            rec.terminal_record_id, Cleanup(status="pending", attempts=1)
        )
        assert await store.update_terminal_cleanup(
            rec.terminal_record_id, Cleanup(status="failed", attempts=5)
        )
        assert not await store.update_terminal_cleanup(
            rec.terminal_record_id, Cleanup(status="succeeded", attempts=6)
        )
        async with store._require_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT body, status, attempts FROM terminal_outbox WHERE terminal_record_id = $1",
                rec.terminal_record_id,
            )
        body = TerminalRecord.model_validate_json(row["body"])
        assert (body.cleanup.status, body.cleanup.refs.media) == ("failed", "m1")
        assert body.record_hash == rec.record_hash and body.terminal_phase == "ended"
        assert (row["status"], row["attempts"]) == ("pending", 0)
    finally:
        await store.close()


async def test_backlog_reports_pending_count_and_age():
    store = await open_store()
    try:
        await store.persist_terminal(record())
        count, age = await store.terminal_outbox_backlog()
        assert count >= 1 and age >= 0.0
    finally:
        await store.close()


async def outbox_row(store, rec):
    async with store._require_pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT status, attempts, last_error, lease_token FROM terminal_outbox "
            "WHERE terminal_record_id = $1 AND record_hash = $2",
            rec.terminal_record_id,
            rec.record_hash,
        )


async def claim_one(store, rec, lease):
    claimed = [
        r
        for r in await store.claim_terminal_outbox(1000, lease)
        if r["terminal_record_id"] == rec.terminal_record_id
    ]
    assert len(claimed) == 1
    return claimed[0]


async def test_a_sender_whose_lease_expired_cannot_overwrite_a_newer_outcome():
    store = await open_store()
    try:
        rec = record()
        await store.persist_terminal(rec)
        old = await claim_one(store, rec, 0.0)  # the lease is already over
        new = await claim_one(store, rec, 60.0)  # a second sender takes it
        assert old["lease_token"] != new["lease_token"]
        assert await store.finish_terminal_outbox(
            rec.terminal_record_id, rec.record_hash, new["lease_token"], "delivered"
        )
        # The stale sender now reports a failure: it must change nothing.
        assert not await store.finish_terminal_outbox(
            rec.terminal_record_id,
            rec.record_hash,
            old["lease_token"],
            "pending",
            error="late",
            retry_in=0.0,
        )
        row = await outbox_row(store, rec)
        assert (row["status"], row["last_error"], row["lease_token"]) == ("delivered", None, None)
    finally:
        await store.close()


async def test_delivered_and_rejected_rows_are_never_resurrected_even_with_the_right_token():
    store = await open_store()
    try:
        rec = record()
        await store.persist_terminal(rec)
        claimed = await claim_one(store, rec, 60.0)
        assert await store.finish_terminal_outbox(
            rec.terminal_record_id,
            rec.record_hash,
            claimed["lease_token"],
            "rejected",
            error="http_422",
        )
        assert not await store.finish_terminal_outbox(
            rec.terminal_record_id,
            rec.record_hash,
            claimed["lease_token"],
            "pending",
            error="again",
        )
        assert (await outbox_row(store, rec))["status"] == "rejected"
        assert all(
            r["terminal_record_id"] != rec.terminal_record_id
            for r in await store.claim_terminal_outbox(1000, 0.0)
        )
    finally:
        await store.close()


async def test_a_late_evidence_row_is_delivered_separately_from_the_primary():
    store = await open_store()
    try:
        ended = record()
        failed = record(
            identity=ended.identity,
            terminal_phase="failed",
            reason_code="execution_failed",
            failure_class="media_failed",
            business_outcome="FAILED",
        )
        await store.persist_terminal(ended)
        await store.persist_terminal(failed)
        sent = []

        async def post(url, body, headers):
            if ended.terminal_record_id.encode() in body:
                sent.append(TerminalRecord.model_validate_json(body).terminal_phase)
                return (
                    (409, {"data": {"code": "already_terminal"}})
                    if b"execution_failed" in body
                    else (201, None)
                )
            return 201, None

        outbox = TerminalOutbox(
            store, TerminalSettings(True, "https://api.example/r", "s"), post=post
        )
        for _ in range(3):
            await outbox.deliver_due()
        assert sorted(sent) == ["ended", "failed"]
        assert (await outbox_row(store, ended))["status"] == "delivered"
        assert (await outbox_row(store, failed))["status"] == "delivered"
    finally:
        await store.close()


async def test_registered_executions_without_a_record_are_listed_and_a_deferral_is_audited():
    store = await open_store()
    try:
        rec = record()
        await store.register_terminal_execution(rec.identity)
        await store.register_terminal_execution(rec.identity)  # idempotent
        assert await store.get_terminal_execution(rec.identity.runtime_session_id) == rec.identity
        assert await store.get_terminal_execution("rt-unknown") is None
        listed = await store.list_unterminated_executions()
        assert rec.identity in listed
        await store.persist_terminal(rec)
        assert rec.identity not in await store.list_unterminated_executions()
        await store.defer_terminal(
            rec.identity.runtime_session_id, "hot_state_missing_unregistered"
        )
        await store.defer_terminal(
            rec.identity.runtime_session_id, "hot_state_missing_unregistered"
        )
        async with store._require_pool().acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM terminal_deferrals WHERE runtime_session_id = $1",
                rec.identity.runtime_session_id,
            )
        assert n == 1
    finally:
        await store.close()
