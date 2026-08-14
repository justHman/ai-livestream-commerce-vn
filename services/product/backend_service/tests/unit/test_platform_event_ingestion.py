"""Service-level tests for PlatformEventIngestionService (OpenSpec 2.3-2.8).

Covers: multi-platform batches, bursts, idempotent duplicates (no double
persist), retry-after-timeout, reordered delivery, structural rejection,
non-comment signals (never embedded/queued), and stable unique-viewer keys.
"""

from __future__ import annotations

import time

import pytest

from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.platform_events import PlatformEvent
from backend.application.platform_events.ingestion import PlatformEventIngestionService
from backend.application.reducer import AcceptedComment, FastReducer, FastReducerConfig


def _event(
    event_id: str,
    etype: str = "viewer.comment",
    platform: str = "tiktok",
    text: str | None = None,
    viewer_id: str = "u1",
    occurred_at: float | None = None,
    payload: dict | None = None,
) -> dict:
    body = {
        "event_id": event_id,
        "platform": platform,
        "source_stream_id": "stream-1",
        "occurred_at": occurred_at if occurred_at is not None else time.time(),
        "type": etype,
        "payload": payload if payload is not None else ({"text": text} if text else {}),
    }
    if viewer_id is not None:
        body["viewer"] = {"viewer_id": viewer_id}
    return body


async def _fresh_service(**kwargs) -> tuple[PlatformEventIngestionService, InMemorySessionStore]:
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, **kwargs)
    return service, store


@pytest.mark.asyncio
async def test_multi_platform_batch_all_accepted() -> None:
    service, _ = await _fresh_service()
    events = [
        PlatformEvent(**_event("e1", platform="tiktok", text="giá bao nhiêu")),
        PlatformEvent(**_event("e2", platform="shopee", text="ship miễn phí không")),
        PlatformEvent(**_event("e3", platform="facebook", text="màu đen có không")),
    ]
    result = await service.ingest("s1", events)

    assert result["accepted"] == 3
    assert result["duplicate"] == 0
    assert result["rejected"] == 0
    assert [item["status"] for item in result["events"]] == ["accepted"] * 3


@pytest.mark.asyncio
async def test_burst_100_events_all_accepted() -> None:
    service, _ = await _fresh_service()
    events = [PlatformEvent(**_event(f"burst-{i}", text=f"comment {i}")) for i in range(100)]
    result = await service.ingest("s1", events)

    assert result["accepted"] == 100
    assert len(result["events"]) == 100


@pytest.mark.asyncio
async def test_duplicate_event_id_is_idempotent_no_double_persist() -> None:
    class _FakePg:
        enabled = True
        inserts = 0

        async def insert_viewer_msg(self, *args, **kwargs):
            _FakePg.inserts += 1

    service, _ = await _fresh_service(pg_store=_FakePg())
    event = PlatformEvent(**_event("dup-1", text="hello"))
    first = await service.ingest("s1", [event])
    second = await service.ingest("s1", [event])

    assert first["events"][0]["status"] == "accepted"
    assert second["events"][0]["status"] == "duplicate"
    assert second["accepted"] == 0
    assert _FakePg.inserts == 1


@pytest.mark.asyncio
async def test_retry_within_window_is_duplicate() -> None:
    now = time.time()
    service, _ = await _fresh_service(dedup_window_sec=3600.0, now_fn=lambda: now)
    event = PlatformEvent(**{**_event("retry-1", text="hi"), "occurred_at": now})
    await service.ingest("s1", [event])

    service._now = lambda: now + 30.0
    result = await service.ingest("s1", [event])

    assert result["events"][0]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_event_id_forgotten_after_dedup_window_is_accepted_again() -> None:
    now = time.time()
    service, _ = await _fresh_service(dedup_window_sec=60.0, now_fn=lambda: now)
    event = PlatformEvent(**{**_event("retry-2", text="hi"), "occurred_at": now})
    await service.ingest("s1", [event])

    service._now = lambda: now + 120.0
    result = await service.ingest("s1", [event])

    # Bounded dedup: ids older than the window are evicted, so a replay
    # after the window is treated as a fresh event again.
    assert result["events"][0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_reordered_delivery_all_accepted_order_independent() -> None:
    service, _ = await _fresh_service()
    now = time.time()
    events = [
        PlatformEvent(
            **{
                **_event("late", text="late comment", viewer_id="u2"),
                "occurred_at": now - 60.0,
            }
        ),
        PlatformEvent(
            **{
                **_event("early", text="early comment", viewer_id="u1"),
                "occurred_at": now - 300.0,
            }
        ),
    ]
    result = await service.ingest("s1", events)

    assert result["accepted"] == 2
    assert result["events"][0]["event_id"] == "late"
    assert result["events"][0]["status"] == "accepted"
    assert result["events"][1]["event_id"] == "early"
    assert result["events"][1]["status"] == "accepted"


@pytest.mark.asyncio
async def test_stale_occurred_at_is_rejected() -> None:
    service, _ = await _fresh_service()
    now = time.time()
    stale = PlatformEvent(**{**_event("stale-1", text="old"), "occurred_at": now - 3600 * 24 * 2})
    future = PlatformEvent(**{**_event("future-1", text="new"), "occurred_at": now + 3600 * 24 * 2})
    result = await service.ingest("s1", [stale, future])

    assert result["rejected"] == 2
    assert [item["reason"] for item in result["events"]] == [
        "occurred_at_out_of_range",
        "occurred_at_out_of_range",
    ]


@pytest.mark.asyncio
async def test_unknown_event_type_is_rejected_at_boundary() -> None:
    service, _ = await _fresh_service()
    with pytest.raises(Exception):
        PlatformEvent(**{**_event("bad-1"), "type": "viewer.typing"})


@pytest.mark.asyncio
async def test_non_comment_events_update_signals_without_embedding() -> None:
    class _FakeCoordinator:
        def __init__(self) -> None:
            self.calls = []

        def has(self, session_id: str) -> bool:
            return True

        def ingest(self, session_id, text, author, ts=None):
            self.calls.append((session_id, text))
            return None

    coordinator = _FakeCoordinator()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator)
    events = [
        PlatformEvent(**{**_event("j1", "viewer.join", payload={"count": 2})}),
        PlatformEvent(**{**_event("f1", "viewer.follow")}),
        PlatformEvent(**{**_event("l1", "viewer.like", payload={"count": 5})}),
    ]
    result = await service.ingest("s1", events)
    meta = await store.get("s1")

    assert result["accepted"] == 3
    assert coordinator.calls == []  # never routed to the semantic pipeline
    assert meta["signal_counts"] == {"join": 2, "follow": 1, "like": 5}
    # Unique viewers from join/follow/like still feed identity normalization.
    assert meta["unique_viewer_ids"] == ["tiktok:stream-1:u1"]


@pytest.mark.asyncio
async def test_comment_without_viewer_is_accepted_without_unique_key() -> None:
    service, _ = await _fresh_service()
    result = await service.ingest(
        "s1",
        [
            PlatformEvent(
                **{
                    **_event("noviewer-1", text="hello"),
                    "viewer": None,
                }
            )
        ],
    )

    assert result["events"][0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_unique_viewer_key_normalization_across_events() -> None:
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store)
    await service.ingest(
        "s1",
        [
            PlatformEvent(**{**_event("k1", text="a", viewer_id="v1")}),
            PlatformEvent(**{**_event("k2", text="b", viewer_id="v1")}),
            PlatformEvent(**{**_event("k3", text="c", viewer_id="v2")}),
        ],
    )
    meta = await store.get("s1")

    assert meta["unique_viewer_ids"] == [
        "tiktok:stream-1:v1",
        "tiktok:stream-1:v2",
    ]


@pytest.mark.asyncio
async def test_unknown_session_raises_keyerror() -> None:
    service, _ = await _fresh_service()
    with pytest.raises(KeyError):
        await service.ingest("missing", [PlatformEvent(**{**_event("x", text="hi")})])


@pytest.mark.asyncio
async def test_stats_expose_sanitized_rejection_counters() -> None:
    service, _ = await _fresh_service()
    now = time.time()
    await service.ingest(
        "s1",
        [
            PlatformEvent(**{**_event("r1", text="old"), "occurred_at": now - 3600 * 24 * 2}),
            PlatformEvent(**{**_event("r2", text="old2"), "occurred_at": now - 3600 * 24 * 2}),
        ],
    )
    stats = service.stats("s1")

    assert stats["rejected_by_reason"] == {"occurred_at_out_of_range": 2}


@pytest.mark.asyncio
async def test_comment_queued_via_coordinator_returns_comment_id() -> None:
    class _FakeCoordinator:
        def __init__(self) -> None:
            self.ingested = []

        def has(self, session_id: str) -> bool:
            return True

        def ingest(self, session_id, text, author, ts=None):
            self.ingested.append((session_id, text, author, ts))
            return type("C", (), {"id": "comment-42"})()

    coordinator = _FakeCoordinator()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator)
    result = await service.ingest(
        "s1",
        [
            PlatformEvent(
                **{
                    **_event("q1", text="giá bao nhiêu", viewer_id="v9"),
                    "viewer": {"viewer_id": "v9", "display_name": "Minh"},
                }
            )
        ],
    )

    assert result["events"][0]["comment_id"] == "comment-42"
    assert coordinator.ingested == [
        ("s1", "giá bao nhiêu", "Minh", pytest.approx(time.time(), abs=5))
    ]


@pytest.mark.asyncio
async def test_comment_parked_on_meta_when_no_coordinator() -> None:
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store)
    result = await service.ingest("s1", [PlatformEvent(**{**_event("p1", text="hi")})])
    meta = await store.get("s1")
    assert result["events"][0]["status"] == "accepted"
    assert "comment_id" not in result["events"][0]
    pending = meta["pending_platform_chat"]

    assert pending[0]["event_id"] == "p1"
    assert pending[0]["text"] == "hi"


# ---------------------------------------------------------------------------
# FastReducer wakeup seam (OpenSpec 4.1): accepted comments notify, duplicates
# and rejected events never do.
# ---------------------------------------------------------------------------


class _RecordingReducer:
    """Boundary stand-in for FastReducer: records notify payloads."""

    def __init__(self) -> None:
        self.notified: list[tuple[str, AcceptedComment]] = []

    def notify_new_events(self, session_id: str, comment: AcceptedComment | None = None) -> None:
        self.notified.append((session_id, comment))


def _make_reducer_wired_service(store, reducer) -> PlatformEventIngestionService:
    return PlatformEventIngestionService(store=store, reducer=reducer)


@pytest.mark.asyncio
async def test_accepted_comment_wakes_reducer_with_full_payload() -> None:
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    reducer = _RecordingReducer()
    service = _make_reducer_wired_service(store, reducer)
    now = time.time()
    await service.ingest(
        "s1",
        [
            PlatformEvent(
                **{
                    **_event("w1", text="giá bao nhiêu", viewer_id="v7"),
                    "occurred_at": now,
                }
            )
        ],
    )

    assert len(reducer.notified) == 1
    _, comment = reducer.notified[0]
    assert comment.event_id == "w1"
    assert comment.comment_id == "w1"  # parked path: falls back to event_id
    assert comment.text == "giá bao nhiêu"
    assert comment.ts == now
    assert comment.viewer_key == "tiktok:stream-1:v7"


@pytest.mark.asyncio
async def test_duplicate_and_rejected_events_do_not_wake_reducer() -> None:
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    reducer = _RecordingReducer()
    service = _make_reducer_wired_service(store, reducer)
    now = time.time()
    event = PlatformEvent(**{**_event("d1", text="hi"), "occurred_at": now})
    stale = PlatformEvent(**{**_event("r1", text="old"), "occurred_at": now - 3600 * 24 * 2})

    await service.ingest("s1", [event, event, stale])

    # Only the FIRST d1 is accepted and notified; the duplicate and the
    # rejected stale event must not notify again.
    assert [(sid, c.event_id) for sid, c in reducer.notified] == [("s1", "d1")]


@pytest.mark.asyncio
async def test_reducer_wake_notifications_and_pending_after_accepted_comment() -> None:
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    reducer = FastReducer(
        config=FastReducerConfig(microbatch_max_wait_ms=300),
        embedder=_FakeReducerEmbedder(),
        now_fn=lambda: time.time(),
    )
    service = _make_reducer_wired_service(store, reducer)
    await service.ingest("s1", [PlatformEvent(**{**_event("f1", text="hello")})])

    assert reducer.stats("s1")["wake_notifications"] == 1
    pending = reducer.drain_batch("s1", time.time())
    assert pending[0].event_id == "f1"
    assert pending[0].text == "hello"


class _FakeReducerEmbedder:
    """No-op embedder so FastReducer constructs without a real model."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]
