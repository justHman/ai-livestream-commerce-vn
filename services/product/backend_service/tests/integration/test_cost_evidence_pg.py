"""M7.3 cost-evidence outbox rows and the one-shot CHECK migration, on a disposable PostgreSQL.

Same gate as test_usage_evidence_pg.py: set USAGE_EVIDENCE_TEST_DATABASE_URL (loopback,
database name livento_017_*). Skipped (NOT a pass) when absent.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.execution_contract import ExecutionIdentity
from backend.application.usage_evidence import envelope
from backend.application.usage_evidence.outbox import UsageOutbox

URL = os.environ.get("USAGE_EVIDENCE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="set USAGE_EVIDENCE_TEST_DATABASE_URL")
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
OLD_CHECK = "CHECK (event_type IN ('ai.execution.usage_evidence', 'ai.usage.reported'))"


@pytest.fixture
async def pg():
    store = PostgresRuntimeStore(URL)
    await store.connect()
    await store.apply_usage_evidence_schema()
    async with store._require_pool().acquire() as conn:
        await conn.execute("TRUNCATE usage_evidence_outbox, usage_evidence_sequence")
    yield store
    await store.close()


def ident() -> ExecutionIdentity:
    return ExecutionIdentity(
        tenant_id=str(uuid.uuid4()),
        business_session_id="biz-" + uuid.uuid4().hex,
        runtime_session_id="rt-" + uuid.uuid4().hex,
        generation="g1",
    )


async def put(outbox, who, kind, key, tokens=1):
    eid, body = envelope.build_cost_body(
        who,
        cost_kind=kind,
        cost_key=key,
        occurred_at=NOW,
        provider="p",
        model="m",
        usage_source="provider",
        fields={"input_tokens": tokens, "output_tokens": 1}
        if kind == "LLM"
        else {"characters": tokens},
        producer_id="runtime",
    )
    return await outbox.enqueue_cost(
        who, event_id=eid, cost_kind=kind, cost_key=key, occurred_at=NOW, body=body
    )


async def test_kinds_replay_and_conflict_on_the_real_unique_key(pg):
    outbox, who = UsageOutbox(pg), ident()
    assert await put(outbox, who, "LLM", "k") == "stored"
    assert await put(outbox, who, "TTS", "k") == "stored"  # same cost_key, other kind
    assert await put(outbox, who, "LLM", "k") == "duplicate"  # replay
    assert await put(outbox, who, "LLM", "k", tokens=5) == "conflict"  # different bytes
    for phrase in ("u:0:1", "u:1:1", "u:2:1"):  # several TTS calls of one utterance
        assert await put(outbox, who, "TTS", phrase) == "stored"
    async with pg._require_pool().acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM usage_evidence_outbox WHERE tenant_id=$1 AND status='ready'",
            who.tenant_id,
        )
    assert n == 5


async def test_check_migration_is_one_shot_transactional_and_idempotent(pg):
    async with pg._require_pool().acquire() as conn:  # simulate a pre-M7.3 table
        await conn.execute(
            "ALTER TABLE usage_evidence_outbox DROP CONSTRAINT usage_evidence_outbox_event_type_check"
        )
        await conn.execute(
            "ALTER TABLE usage_evidence_outbox ADD CONSTRAINT usage_evidence_outbox_event_type_check "
            + OLD_CHECK
        )
    assert not await pg.usage_cost_event_type_ready()
    await pg.apply_usage_evidence_schema()  # startup path must NOT widen it
    assert not await pg.usage_cost_event_type_ready()
    assert await pg.migrate_usage_cost_event_type() is True
    assert await pg.usage_cost_event_type_ready()
    assert await pg.migrate_usage_cost_event_type() is False
    assert await put(UsageOutbox(pg), ident(), "LLM", "k") == "stored"
