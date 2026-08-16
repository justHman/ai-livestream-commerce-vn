"""Concurrency tests for event dedup atomicity (P1-04).

The ingestion service dedups ``event_id`` against a bounded session-scoped
index that lives in session meta. Two concurrent ``ingest()`` calls for the
same session must serialize the read -> decide -> write critical section, so a
replayed event id is accepted exactly once even under concurrent ingress.

The fake store gates ``get()`` so BOTH concurrent ``ingest()`` calls observe
the SAME pre-event meta (otherwise the race is invisible to a deterministic
test). On the serialized (per-session locked) path only one ``get()`` is ever
in flight, so the first proceeds after a short grace and the second later
reads the updated meta.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.application.platform_events import PlatformEvent
from backend.application.platform_events.ingestion import PlatformEventIngestionService
from backend.application.reducer import AcceptedComment


def _event(
    event_id: str,
    text: str,
    viewer_id: str = "u1",
    occurred_at: float | None = None,
) -> dict:
    body = {
        "event_id": event_id,
        "platform": "tiktok",
        "source_stream_id": "stream-1",
        "occurred_at": occurred_at if occurred_at is not None else time.time(),
        "type": "viewer.comment",
        "payload": {"text": text},
    }
    if viewer_id is not None:
        body["viewer"] = {"viewer_id": viewer_id}
    return body


class _BarrierStore:
    """SessionStore stand-in that forces concurrent reads onto one pre-event state.

    ``get()`` captures the meta snapshot at the moment it arrives at a 2-party
    barrier, then waits for the other ``get()`` to arrive before returning.
    Both concurrent ``ingest()`` calls therefore read the SAME pre-event meta,
    even though after the barrier releases one task can race ahead and write
    before the other resumes (the snapshot is already captured). When the
    critical section is serialized by a per-session lock, only one ``get()``
    is ever in flight: the first waiter times out and proceeds, breaking the
    barrier so the second ``get()`` returns immediately. Every ``set()`` is
    recorded so the test can assert the write happened.
    """

    def __init__(self, meta: dict) -> None:
        self._meta = dict(meta)
        self._barrier = asyncio.Barrier(2)
        self.sets: list[dict] = []

    async def get(self, session_id: str) -> dict:
        snapshot = dict(self._meta)
        try:
            await asyncio.wait_for(self._barrier.wait(), timeout=0.5)
        except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
            pass
        return snapshot

    async def set(self, session_id: str, meta: dict) -> None:
        self._meta = dict(meta)
        self.sets.append(dict(meta))

    async def exists(self, session_id: str) -> bool:
        return True


class _RecordingReducer:
    """Boundary stand-in for FastReducer: records notify payloads."""

    def __init__(self) -> None:
        self.notified: list[tuple[str, AcceptedComment]] = []

    def notify_new_events(self, session_id: str, comment: AcceptedComment | None = None) -> None:
        self.notified.append((session_id, comment))


def _seen_ids(meta: dict) -> list[str]:
    return [entry["event_id"] for entry in (meta.get("platform_event_ids") or [])]


@pytest.mark.asyncio
async def test_concurrent_same_event_id_accepted_exactly_once() -> None:
    store = _BarrierStore({"status": "active"})
    reducer = _RecordingReducer()
    service = PlatformEventIngestionService(store=store, reducer=reducer)
    now = time.time()
    event = PlatformEvent(**{**_event("dup-concurrent", text="hello"), "occurred_at": now})

    async def ingest_once() -> dict:
        return await service.ingest("s1", [event])

    results = await asyncio.gather(ingest_once(), ingest_once())

    statuses = sorted(item["status"] for r in results for item in r["events"])
    assert statuses == ["accepted", "duplicate"]
    assert [(sid, c.event_id) for sid, c in reducer.notified] == [("s1", "dup-concurrent")]
    assert _seen_ids(await store.get("s1")) == ["dup-concurrent"]


@pytest.mark.asyncio
async def test_concurrent_distinct_event_ids_both_persisted() -> None:
    store = _BarrierStore({"status": "active"})
    service = PlatformEventIngestionService(store=store)
    now = time.time()
    event_a = PlatformEvent(**{**_event("conc-a", text="hello a"), "occurred_at": now})
    event_b = PlatformEvent(**{**_event("conc-b", text="hello b"), "occurred_at": now})

    async def ingest_one(event: PlatformEvent) -> dict:
        return await service.ingest("s1", [event])

    results = await asyncio.gather(ingest_one(event_a), ingest_one(event_b))

    assert all(r["accepted"] == 1 for r in results)
    assert _seen_ids(await store.get("s1")) == ["conc-a", "conc-b"]
