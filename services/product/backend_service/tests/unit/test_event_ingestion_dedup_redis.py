"""Cross-process (Redis-backed) event dedup tests for P1-04.

Two ``PlatformEventIngestionService`` instances sharing one Redis-backed store
simulate two backend tasks (rolling deploy via ``deployment_maximum_percent``,
autoscale, operator) that the per-process ``asyncio.Lock`` cannot coordinate.
The durable fix is a Redis ``SET NX`` per-session lock serializing the WHOLE
``ingest()`` critical section — real cross-process mutual exclusion that fixes
BOTH duplicate acceptance AND lost concurrent metadata updates.

The fake redis client below IS one Redis "server": two ``RedisSessionStore``
instances wrapping the SAME fake client behave like two backend processes
connected to one Redis. On the current HEAD (in-process lock only) both
instances read the same pre-event meta and both accept the duplicate, and two
distinct concurrent events clobber each other (last-write-wins).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.application.db.redis_session_store import RedisSessionStore
from backend.application.db.session_store import SessionLockTimeout
from backend.application.platform_events import PlatformEvent
from backend.application.platform_events.ingestion import PlatformEventIngestionService
from backend.application.reducer import AcceptedComment


def _event(
    event_id: str,
    text: str | None = None,
    etype: str = "viewer.comment",
    viewer_id: str = "u1",
    occurred_at: float | None = None,
    payload: dict | None = None,
) -> dict:
    body = {
        "event_id": event_id,
        "platform": "tiktok",
        "source_stream_id": "stream-1",
        "occurred_at": occurred_at if occurred_at is not None else time.time(),
        "type": etype,
        "payload": payload if payload is not None else ({"text": text} if text else {}),
    }
    if viewer_id is not None:
        body["viewer"] = {"viewer_id": viewer_id}
    return body


class _FakeRedis:
    """Minimal async redis stand-in: session blob + SET NX PX lock primitives.

    One instance == one Redis server. ``set(nx=True)`` is atomic (no await in
    the body), so exactly one caller wins the lock — mirroring Redis SET NX.
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    async def set(self, key: str, value, *, ex=None, nx=False, px=None) -> bool | None:
        raw = value.encode() if isinstance(value, str) else value
        if nx and key in self._data:
            return None
        self._data[key] = raw
        return True

    async def delete(self, key: str) -> int:
        return 1 if self._data.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self._data else 0

    async def eval(self, script: str, numkeys: int, *args) -> int:
        keys, rest = args[:numkeys], args[numkeys:]
        if self._data.get(keys[0]) == rest[0].encode():
            del self._data[keys[0]]
            return 1
        return 0


class _RecordingReducer:
    """Boundary stand-in for FastReducer: records notify payloads."""

    def __init__(self) -> None:
        self.notified: list[tuple[str, AcceptedComment]] = []

    def notify_new_events(self, session_id: str, comment: AcceptedComment | None = None) -> None:
        self.notified.append((session_id, comment))


def _seen_ids(meta: dict | None) -> list[str]:
    return [entry["event_id"] for entry in (meta.get("platform_event_ids") or [])]


def _two_services(
    fake: _FakeRedis,
) -> tuple[PlatformEventIngestionService, PlatformEventIngestionService, RedisSessionStore]:
    """Two service instances, each with its own in-process lock + own store.

    Both stores wrap the SAME fake client -> one shared Redis. The only
    coordination between the two "processes" is the distributed lock.
    """
    store_a = RedisSessionStore(client=fake)
    store_b = RedisSessionStore(client=fake)
    service_a = PlatformEventIngestionService(store=store_a, lock_acquire_timeout_seconds=0.5)
    service_b = PlatformEventIngestionService(store=store_b, lock_acquire_timeout_seconds=0.5)
    return service_a, service_b, store_a


@pytest.mark.asyncio
async def test_cross_process_same_event_id_accepted_exactly_once() -> None:
    fake = _FakeRedis()
    service_a, service_b, store_a = _two_services(fake)
    await store_a.set("s1", {"status": "active"})
    reducer_a = _RecordingReducer()
    reducer_b = _RecordingReducer()
    service_a._reducer = reducer_a
    service_b._reducer = reducer_b
    now = time.time()
    event = PlatformEvent(**{**_event("dup-cross", text="hello"), "occurred_at": now})

    async def ingest_on(service: PlatformEventIngestionService) -> dict:
        return await service.ingest("s1", [event])

    results = await asyncio.gather(ingest_on(service_a), ingest_on(service_b))

    statuses = sorted(item["status"] for r in results for item in r["events"])
    assert statuses == ["accepted", "duplicate"]
    # Side effects fire exactly once across the two "processes".
    assert len(reducer_a.notified) + len(reducer_b.notified) == 1
    assert _seen_ids(await store_a.get("s1")) == ["dup-cross"]


@pytest.mark.asyncio
async def test_cross_process_distinct_events_no_lost_updates() -> None:
    fake = _FakeRedis()
    service_a, service_b, store_a = _two_services(fake)
    await store_a.set("s1", {"status": "active"})
    now = time.time()
    comment = PlatformEvent(**{**_event("conc-a", text="hello a"), "occurred_at": now})
    follow = PlatformEvent(
        **{**_event("conc-b", etype="viewer.follow", payload={"count": 1}), "occurred_at": now}
    )

    async def ingest_on(service: PlatformEventIngestionService, ev: PlatformEvent) -> dict:
        return await service.ingest("s1", [ev])

    results = await asyncio.gather(ingest_on(service_a, comment), ingest_on(service_b, follow))

    assert all(r["accepted"] == 1 for r in results)
    meta = await store_a.get("s1")
    # BOTH updates survive: comment dedup + follow signal + viewer identity.
    assert set(_seen_ids(meta)) == {"conc-a", "conc-b"}
    assert meta["signal_counts"] == {"follow": 1}
    assert meta["unique_viewer_ids"] == ["tiktok:stream-1:u1"]


@pytest.mark.asyncio
async def test_cross_process_lock_timeout_raises_typed_error() -> None:
    fake = _FakeRedis()
    store = RedisSessionStore(client=fake)
    await store.set("s1", {"status": "active"})
    reducer = _RecordingReducer()
    service = PlatformEventIngestionService(
        store=store, reducer=reducer, lock_acquire_timeout_seconds=0.05
    )
    assert await store.acquire_session_lock("s1", "holder-token", ttl_seconds=10)

    now = time.time()
    event = PlatformEvent(**{**_event("timeout-event", text="hi"), "occurred_at": now})
    with pytest.raises(SessionLockTimeout):
        await service.ingest("s1", [event])

    # Never processed unlocked: no reducer side effect, no dedup write.
    assert reducer.notified == []
    assert _seen_ids(await store.get("s1")) == []


@pytest.mark.asyncio
async def test_with_session_lock_releases_on_success() -> None:
    store = RedisSessionStore(client=_FakeRedis())
    async with store.with_session_lock("s1", ttl_seconds=10):
        assert await store.acquire_session_lock("s1", "other", ttl_seconds=10) is False
    assert await store.acquire_session_lock("s1", "new-holder", ttl_seconds=10) is True


@pytest.mark.asyncio
async def test_release_only_deletes_own_token() -> None:
    fake = _FakeRedis()
    store = RedisSessionStore(client=fake)
    assert await store.acquire_session_lock("s1", "token-a", ttl_seconds=10) is True
    await store.release_session_lock("s1", "token-a")
    assert await store.acquire_session_lock("s1", "token-b", ttl_seconds=10) is True
    # A stale release (old token) must not delete the newer holder's lock.
    await store.release_session_lock("s1", "token-a")
    assert await store.acquire_session_lock("s1", "token-c", ttl_seconds=10) is False


@pytest.mark.asyncio
async def test_with_session_lock_times_out_bounded() -> None:
    store = RedisSessionStore(client=_FakeRedis())
    assert await store.acquire_session_lock("s1", "holder", ttl_seconds=10) is True
    with pytest.raises(SessionLockTimeout):
        async with store.with_session_lock("s1", acquire_timeout_seconds=0.05):
            pass  # pragma: no cover


def test_sessions_events_lock_timeout_returns_503() -> None:
    """Route maps a held session lock to 503, never a generic 500."""
    from fastapi.testclient import TestClient

    from backend.application.platform_events import PlatformEventIngestionService
    from backend.config import AppConfig
    from conftest import make_deps as _Deps

    async def _seed(store: RedisSessionStore) -> None:
        await store.set("s1", {"status": "active"})
        await store.acquire_session_lock("s1", "holder-token", ttl_seconds=10)

    fake = _FakeRedis()
    store = RedisSessionStore(client=fake)
    asyncio.run(_seed(store))

    config = AppConfig(render_backend="mock", app_env="dev")
    deps = _Deps(
        config=config,
        store=store,
        event_ingestion=PlatformEventIngestionService(
            store=store, lock_acquire_timeout_seconds=0.05
        ),
    )
    from backend.main import create_app

    with TestClient(create_app(config=config, deps=deps)) as client:
        r = client.post("/api/v1/sessions/s1/events", json={"events": [_event("e1", text="hello")]})

    assert r.status_code == 503
