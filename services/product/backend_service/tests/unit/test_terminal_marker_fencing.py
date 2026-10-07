import copy

import pytest
from fastapi import HTTPException

from backend.api.v1.sessions import _stop_cancelled_session
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.terminal_outcomes import PENDING_FLAG, TerminalOutcomes
from backend.application.usage_evidence import EVIDENCE_TTL
from .test_terminal_outcomes import FakePg, hot, stop_container


class FencedStore(InMemorySessionStore):
    owner = "old"

    async def commit_if_owner(self, fence, meta, **kw):
        if fence != self.owner:
            return False
        await self.set("s", copy.deepcopy(meta), **kw)
        self.last_ttl = kw.get("ttl_seconds")
        return True


async def test_terminal_persist_error_under_expired_fence_preserves_new_owner():
    d = stop_container([])
    d.store = FencedStore()
    d.backend.stop = lambda sid: None
    old = hot("ending")
    new = hot("failed", usage_terminal_unstaged={"proof": "only-copy"})
    new["execution_contract"]["sequence"] = 8
    await d.store.set("s", copy.deepcopy(old))

    class PG(FakePg):
        async def persist_terminal(self, record):
            d.store.owner = "new"
            await d.store.set("s", copy.deepcopy(new))
            raise ConnectionError("controlled outage")

    d.terminal_outcomes = TerminalOutcomes(PG())
    with pytest.raises(HTTPException) as caught:
        await _stop_cancelled_session(d, "s", "old")
    assert caught.value.status_code == 503
    assert await d.store.get("s") == new


async def test_cleanup_retry_marker_under_expired_fence_keeps_usage_proof():
    d = stop_container([])
    d.store = FencedStore()
    d.backend.stop = lambda sid: None
    newer = hot("failed", usage_terminal_unstaged={"proof": "only-copy"})
    newer["execution_contract"]["sequence"] = 8
    await d.store.set("s", newer)
    d.store.owner = "new"

    class Publishers:
        async def stop(self, sid):
            raise RuntimeError("controlled cleanup error")

    d.livekit_publishers = Publishers()
    d.terminal_outcomes = TerminalOutcomes(FakePg())
    with pytest.raises(HTTPException) as caught:
        await _stop_cancelled_session(d, "s", "old")
    assert caught.value.status_code == 503
    assert await d.store.get("s") == newer


async def test_current_owner_marker_keeps_evidence_and_extended_retention():
    store = FencedStore()
    meta = hot("failed", usage_terminal_unstaged={"proof": "only-copy"})
    await store.set("s", meta)
    await TerminalOutcomes._mark_pending(store, "s", "old")
    current = await store.get("s")
    assert current["usage_terminal_unstaged"] == meta["usage_terminal_unstaged"]
    assert current[PENDING_FLAG] is True
    assert store.last_ttl == EVIDENCE_TTL


async def test_marker_without_fence_cannot_mutate_distributed_store():
    store = FencedStore()
    meta = hot("ending")
    await store.set("s", meta)
    await TerminalOutcomes._mark_pending(store, "s")
    assert await store.get("s") == meta


async def test_marker_does_not_resurrect_deleted_metadata():
    store = FencedStore()
    await TerminalOutcomes._mark_pending(store, "s", "old")
    assert await store.get("s") is None
