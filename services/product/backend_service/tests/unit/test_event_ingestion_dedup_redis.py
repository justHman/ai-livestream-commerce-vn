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

from backend.application.db.redis_session_store import RedisSessionStore, SessionLockFence
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
    Expiry is clock-driven via ``now`` (monotonic seconds, default 1000.0) so
    TTL overrun is deterministic without real sleeps; expired keys read as
    absent everywhere.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self._data: dict[str, bytes] = {}
        self._exp: dict[str, float] = {}
        self.now = now

    def _expire_if_past(self, key: str) -> None:
        if key in self._data and self.now >= self._exp.get(key, float("inf")):
            del self._data[key]
            self._exp.pop(key, None)

    async def get(self, key: str) -> bytes | None:
        self._expire_if_past(key)
        return self._data.get(key)

    async def set(self, key: str, value, *, ex=None, nx=False, px=None) -> bool | None:
        self._expire_if_past(key)
        if nx and key in self._data:
            return None
        raw = value.encode() if isinstance(value, str) else value
        self._data[key] = raw
        ttl_ms = px if px is not None else (ex * 1000 if ex is not None else None)
        if ttl_ms is not None:
            self._exp[key] = self.now + ttl_ms / 1000.0
        else:
            self._exp.pop(key, None)
        return True

    async def delete(self, key: str) -> int:
        self._expire_if_past(key)
        return 1 if self._data.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        self._expire_if_past(key)
        return 1 if key in self._data else 0

    async def eval(self, script: str, numkeys: int, *args) -> int:
        keys, rest = args[:numkeys], args[numkeys:]
        lock_key = keys[0]
        self._expire_if_past(lock_key)
        if "KEYS[2]" in script:  # commit-if-owner: also writes the session blob
            if self._data.get(lock_key) == rest[0].encode():
                self._data[keys[1]] = rest[1].encode()
                self._exp[keys[1]] = self.now + int(rest[2])
                return 1
            return 0
        # release-script: compare-and-delete the lock.
        if self._data.get(lock_key) == rest[0].encode():
            del self._data[lock_key]
            self._exp.pop(lock_key, None)
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


@pytest.mark.asyncio
async def test_stale_owner_write_rejected_after_lock_takeover() -> None:
    fake = _FakeRedis()
    store = RedisSessionStore(client=fake)
    token_a = "token-a"
    assert await store.acquire_session_lock("s1", token_a, ttl_seconds=10)
    fence_a = SessionLockFence(session_id="s1", token=token_a)
    fake.now += 11  # A overruns its 10s lock TTL
    token_b = "token-b"
    assert await store.acquire_session_lock("s1", token_b, ttl_seconds=10) is True
    fence_b = SessionLockFence(session_id="s1", token=token_b)
    assert await store.commit_if_owner(fence_b, {"status": "b"}) is True
    assert await store.commit_if_owner(fence_a, {"status": "a-stale"}) is False
    assert await store.get("s1") == {"status": "b"}


@pytest.mark.asyncio
async def test_current_owner_write_accepted() -> None:
    fake = _FakeRedis()
    store = RedisSessionStore(client=fake)
    token_a = "token-a"
    assert await store.acquire_session_lock("s1", token_a, ttl_seconds=10)
    fence = SessionLockFence(session_id="s1", token=token_a)
    assert await store.commit_if_owner(fence, {"status": "active"}) is True
    assert await store.get("s1") == {"status": "active"}


@pytest.mark.asyncio
async def test_stale_owner_ingest_maps_to_session_lock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRedis()
    service_a, service_b, store_a = _two_services(fake)
    await store_a.set("s1", {"status": "active"})
    entered = asyncio.Event()
    release = asyncio.Event()
    real_save_meta = service_a._save_meta

    async def gated_save_meta(
        session_id: str, meta: dict, *, fence: SessionLockFence | None = None
    ) -> None:
        entered.set()
        await release.wait()
        await real_save_meta(session_id, meta, fence=fence)

    monkeypatch.setattr(service_a, "_save_meta", gated_save_meta)

    now = time.time()
    ev_a = PlatformEvent(**{**_event("ev-a", text="a"), "occurred_at": now})
    ev_b = PlatformEvent(**{**_event("ev-b", text="b"), "occurred_at": now})
    task = asyncio.create_task(service_a.ingest("s1", [ev_a]))
    await entered.wait()
    fake.now += 11  # A overruns its 10s lock TTL inside the critical section
    await service_b.ingest("s1", [ev_b])  # B acquires the expired lock + commits
    release.set()
    with pytest.raises(SessionLockTimeout):
        await task
    meta = await store_a.get("s1")
    assert _seen_ids(meta) == ["ev-b"]


@pytest.mark.asyncio
async def test_non_fenced_memory_path_unchanged() -> None:
    from backend.application.db.memory_session_store import InMemorySessionStore

    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store)
    now = time.time()
    event = PlatformEvent(**{**_event("mem-1", text="hi"), "occurred_at": now})
    result = await service.ingest("s1", [event])
    assert result["accepted"] == 1
    assert _seen_ids(await store.get("s1")) == ["mem-1"]


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
