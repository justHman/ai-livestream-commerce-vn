"""Service-level tests for PlatformEventIngestionService (OpenSpec 2.3-2.8).

Covers: multi-platform batches, bursts, idempotent duplicates (no double
persist), retry-after-timeout, reordered delivery, structural rejection,
non-comment signals (never embedded/queued), and stable unique-viewer keys.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import time

import pytest

from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.director.coordinator import DirectorCoordinator
from backend.application.director.errors import CoordinatorUnavailable
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


# ---------------------------------------------------------------------------
# P0-FB-013 Task 1 — typed delivery outcomes. Gated behind the explicit
# ``delivery_outcomes_v1`` request opt-in; with the flag off (today's
# deployed API) the legacy accepted+parked contract is unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_without_coordinator_is_not_ready_never_accepted() -> None:
    service, store = await _fresh_service()
    result = await service.ingest(
        "s1", [PlatformEvent(**_event("nr-1", text="hi"))], delivery_outcomes_v1=True
    )
    item = result["events"][0]

    assert item["status"] == "not_ready"
    assert item["status"] != "accepted"
    assert item["reason"] == "no_coordinator_attached"
    assert item["action_identity"] == "nr-1"
    # No parking: a not-ready event leaves nothing behind to be drained.
    stored = await store.get("s1")
    assert stored is None or "pending_platform_chat" not in stored


from .test_approved_speech_active import case_factory as _case_factory  # noqa: E402
from backend.bootstrap.lifespan import _shutdown  # noqa: E402

case_factory = _case_factory  # real-app fixture from the approved-speech suite


class _AttachingCoordinator:
    """Coordinator double whose attach lands between the check and the put.

    ``has()`` is asked twice per comment (session-existence probe, then the
    routing check). The first comment observes "not attached" and is answered
    ``not_ready``; the attach happens right after, so the second comment is
    genuinely routed. No sleeps, no timing — the double decides the outcome.
    """

    def __init__(self) -> None:
        self.attached = False
        self._has_calls = 0
        self.ingested: list[tuple[str, str]] = []

    def has(self, session_id: str) -> bool:
        self._has_calls += 1
        if self._has_calls == 2:
            self.attached = True
        return self.attached

    def ingest(self, session_id, text, author, ts=None):
        self.ingested.append((session_id, text))
        return type("C", (), {"id": "comment-race"})()


@pytest.mark.asyncio
async def test_coordinator_attach_race_yields_routed_not_accepted_without_coordinator() -> None:
    coordinator = _AttachingCoordinator()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator)
    result = await service.ingest(
        "s1",
        [
            PlatformEvent(**_event("race-1", text="first")),
            PlatformEvent(**_event("race-2", text="second")),
        ],
        delivery_outcomes_v1=True,
    )

    before_attach, after_attach = result["events"]
    assert before_attach["status"] == "not_ready"
    assert after_attach["status"] == "routed"
    # Neither leg of the race may be reported as a legacy success.
    assert before_attach["status"] != "accepted"
    assert after_attach["status"] != "accepted"
    assert after_attach["comment_id"] == "comment-race"


class _FullQueueCoordinator:
    """Coordinator whose chat queue has no room left."""

    def __init__(self) -> None:
        self.ingested: list[tuple[str, str]] = []

    def has(self, session_id: str) -> bool:
        return True

    def queue_capacity(self, session_id: str) -> int:
        return 0

    def ingest(self, session_id, text, author, ts=None):
        self.ingested.append((session_id, text))
        return type("C", (), {"id": "comment-evicted"})()


@pytest.mark.asyncio
async def test_full_queue_is_not_reported_as_success() -> None:
    coordinator = _FullQueueCoordinator()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator)
    event = PlatformEvent(**_event("qf-1", text="overflow"))
    result = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    item = result["events"][0]

    # Never a success, and never a silent eviction of someone else's comment.
    assert item["status"] not in ("accepted", "routed")
    assert item["reason"] == "queue_full"
    assert coordinator.ingested == []
    assert "comment_id" not in item
    # Identity preserved: the durable retry can re-drive the same event id.
    assert item["action_identity"] == "qf-1"
    assert (await service.ingest("s1", [event], delivery_outcomes_v1=True))["events"][0][
        "reason"
    ] == "queue_full"


class _AttachableCoordinator:
    """Coordinator double that is absent until ``attach()`` is called."""

    def __init__(self) -> None:
        self.attached = False
        self.ingested: list[str] = []
        self.queued_ts: list[float] = []

    def attach(self) -> None:
        self.attached = True

    def has(self, session_id: str) -> bool:
        return self.attached

    def ingest(self, session_id, text, author, ts: float = 0.0):
        self.ingested.append(text)
        self.queued_ts.append(ts)
        return type("C", (), {"id": "comment-retry"})()


@pytest.mark.asyncio
async def test_not_ready_retry_after_coordinator_attaches_is_accepted() -> None:
    coordinator = _AttachableCoordinator()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator)
    event = PlatformEvent(**_event("retry-nr", text="hi"))

    first = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert first["events"][0]["status"] == "not_ready"

    coordinator.attach()
    second = await service.ingest("s1", [event], delivery_outcomes_v1=True)

    # A not-ready event must not have consumed the dedup identity, or this
    # retry would come back a silent 'duplicate' and the comment is lost.
    assert second["events"][0]["status"] == "routed"
    assert second["events"][0]["comment_id"] == "comment-retry"
    assert coordinator.ingested == ["hi"]
    # ...and the delivery that finally worked is now deduped.
    assert (await service.ingest("s1", [event], delivery_outcomes_v1=True))["events"][0][
        "status"
    ] == "duplicate"


@pytest.mark.asyncio
async def test_default_opt_in_off_keeps_existing_accepted_parking_behaviour() -> None:
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store)
    event = PlatformEvent(**_event("legacy-1", text="hi"))

    result = await service.ingest("s1", [event])
    item = result["events"][0]

    # The deployed API does not opt in, so it must keep seeing exactly the
    # contract it shipped against: no 013 keys, no new vocabulary.
    assert set(item) == {"event_id", "status", "safety"}
    assert item["status"] == "accepted"
    assert result["accepted"] == 1
    legacy_meta = await store.get("s1")
    assert legacy_meta is not None
    assert legacy_meta["pending_platform_chat"][0]["event_id"] == "legacy-1"
    assert (await service.ingest("s1", [event]))["events"][0]["status"] == "duplicate"


# ---------------------------------------------------------------------------
# P0-FB-013 Task 2 — routed work that dies with the coordinator is reconciled
# as audited non_deliverable instead of vanishing into the dropped queue.
# ---------------------------------------------------------------------------


# F3 shipped green because the double below counted with one global int while
# production counts per session. Binding the real method (not re-writing it) is
# what stops the double from holding different semantics than the thing it
# stands in for, whatever the production counter becomes next.
_production_next_delivery_tick = DirectorCoordinator.next_delivery_tick


class _RoutedTeardownCoordinator:
    """Coordinator that routes, then can tear down before consuming."""

    def __init__(self) -> None:
        self.attached = True
        self.ingested: list[str] = []
        # The ts the ingress actually handed over, per routed comment. A
        # delivery-path reset would be visible here and nowhere else.
        self.queued_ts: list[float] = []
        # Same shape as DirectorCoordinator._delivery_seq: PER SESSION.
        self._delivery_seq: dict[str, int] = {}

    def has(self, session_id: str) -> bool:
        return self.attached

    def ingest(self, session_id, text, author, ts: float = 0.0):
        self._delivery_seq[session_id] = self._delivery_seq.get(session_id, 0) + 1
        self.ingested.append(text)
        self.queued_ts.append(ts)
        return type("C", (), {"id": f"comment-{self._delivery_seq[session_id]}"})()

    def next_delivery_tick(self, session_id: str | None = None) -> int:
        """PRODUCTION's counter, bound — not a re-implementation of it."""
        return _production_next_delivery_tick(self, session_id)


class _AuditSink:
    """pg_store stand-in recording the non_deliverable audit rows."""

    enabled = True

    def __init__(self) -> None:
        self.audits: list[tuple[str, dict]] = []

    async def insert_audit_event(self, kind, **kwargs):
        self.audits.append((kind, kwargs))

    async def insert_viewer_msg(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_teardown_after_routing_before_consumption_is_non_deliverable() -> None:
    coordinator = _RoutedTeardownCoordinator()
    audit = _AuditSink()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator, pg_store=audit)
    event = PlatformEvent(**_event("td-1", text="hi"))
    await service.ingest("s1", [event], delivery_outcomes_v1=True)

    # The session dies with the comment still sitting unconsumed in the queue.
    coordinator.attached = False
    reconciled = await service.reconcile_session("s1", attach_seq=coordinator.next_delivery_tick())

    assert reconciled == ["td-1"]
    kind, row = audit.audits[-1]
    assert kind == "event_ingress.non_deliverable"
    assert row["resource"] == "viewer.comment:td-1"
    assert row["detail"]["reason"] == "coordinator_torn_down_before_consumption"
    # Audited, not lost: the reconciled event is terminal and must not be
    # re-offered as if it were still pending.
    assert service.terminal_outcomes("s1")["td-1"].outcome == "non_deliverable"


@pytest.mark.asyncio
async def test_stale_teardown_cannot_overwrite_a_newer_outcome() -> None:
    coordinator = _RoutedTeardownCoordinator()
    audit = _AuditSink()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator, pg_store=audit)
    await service.ingest(
        "s1", [PlatformEvent(**_event("td-2", text="hi"))], delivery_outcomes_v1=True
    )

    # A teardown report that predates the delivery must not clobber it.
    stale = await service.reconcile_session("s1", attach_seq=0)
    assert stale == []

    assert service.terminal_outcomes("s1")["td-2"].outcome == "routed"


@pytest.mark.asyncio
async def test_consumed_comment_is_never_reconciled_as_non_deliverable() -> None:
    coordinator = _RoutedTeardownCoordinator()
    audit = _AuditSink()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator, pg_store=audit)
    await service.ingest(
        "s1", [PlatformEvent(**_event("td-3", text="hi"))], delivery_outcomes_v1=True
    )
    # The coordinator drained the queue before the teardown. The tick reports
    # the ChatQueue comment ids it read, which is what the ledger indexed.
    service.mark_consumed("s1", {"comment-1"})

    assert await service.reconcile_session("s1", attach_seq=0) == []


# ---------------------------------------------------------------------------
# F3 — the teardown fence is PER SESSION. A cross-session stamp against a
# per-session fence means routed work in any second session is skipped forever:
# the entry stays ``routed`` in the ledger and NO audit row is ever written.
# The single-session double above could not see it, so the double now binds
# production's real counter and this test drives the real coordinator.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_session_routed_work_is_reconciled_on_its_own_teardown() -> None:
    """s2's routed comment must be audited non_deliverable on s2 teardown.

    Two sessions route into the SAME coordinator. s1 goes first, so by the
    time s2 is torn down the coordinator's total routed count is 2 while s2's
    own fence is 1. Stamping s2's entry with the cross-session total made
    ``attach_seq > fence`` and it was skipped forever — silent loss.
    """
    from backend.api.v1 import ProductEntityIn
    from backend.application.director.embeddings import HashingEmbedder
    from backend.application.director.session_context import DirectorRuntime
    from avatar.engines.mock import MockRenderBackend

    coordinator = DirectorCoordinator(
        runtime=DirectorRuntime(backend=MockRenderBackend(), embedder=HashingEmbedder()),
        llm=None,
        tts=None,
        backend=None,
    )
    products = [ProductEntityIn(id="P001", name="Kem").to_entity()]
    coordinator.start("s1", products)
    coordinator.start("s2", products)
    try:
        audit = _AuditSink()
        store = InMemorySessionStore()
        await store.set("s1", {"status": "active"})
        await store.set("s2", {"status": "active"})
        service = PlatformEventIngestionService(
            store=store, coordinator=coordinator, pg_store=audit
        )

        assert (
            await service.ingest(
                "s1", [PlatformEvent(**_event("ms-1", text="a"))], delivery_outcomes_v1=True
            )
        )["events"][0]["status"] == "routed"
        assert (
            await service.ingest(
                "s2", [PlatformEvent(**_event("ms-2", text="b"))], delivery_outcomes_v1=True
            )
        )["events"][0]["status"] == "routed"

        # The cross-session total is strictly larger than s2's own fence.
        assert coordinator.next_delivery_tick() == 2
        assert coordinator.next_delivery_tick("s2") == 1

        # s1 tears down first, so the coordinator has really been multi-session.
        s2_fence = coordinator.stop("s2")

        assert s2_fence == 1
        assert await service.reconcile_session("s2", attach_seq=s2_fence) == ["ms-2"]

        reconciled = service.terminal_outcomes("s2")["ms-2"]
        assert reconciled.outcome == "non_deliverable"
        assert reconciled.reason == "coordinator_torn_down_before_consumption"
        kind, row = audit.audits[-1]
        assert kind == "event_ingress.non_deliverable"
        assert row["resource"] == "viewer.comment:ms-2"
    finally:
        coordinator.stop_all()


@pytest.mark.asyncio
async def test_bulk_teardown_reconciles_every_session_its_own_fence() -> None:
    """``stop_all`` shares the same fence contract, so it shares the bug.

    A process shutdown stops every session at once. Each session's routed work
    must be reconciled against ITS OWN fence, or the later-stopped session's
    entries are skipped and die without an audit.
    """
    from backend.api.v1 import ProductEntityIn
    from backend.application.director.embeddings import HashingEmbedder
    from backend.application.director.session_context import DirectorRuntime
    from avatar.engines.mock import MockRenderBackend

    coordinator = DirectorCoordinator(
        runtime=DirectorRuntime(backend=MockRenderBackend(), embedder=HashingEmbedder()),
        llm=None,
        tts=None,
        backend=None,
    )
    products = [ProductEntityIn(id="P001", name="Kem").to_entity()]
    for sid in ("s1", "s2", "s3"):
        coordinator.start(sid, products)
    try:
        audit = _AuditSink()
        store = InMemorySessionStore()
        service = PlatformEventIngestionService(
            store=store, coordinator=coordinator, pg_store=audit
        )
        for index, sid in enumerate(("s1", "s2", "s3"), start=1):
            await store.set(sid, {"status": "active"})
            assert (
                await service.ingest(
                    sid,
                    [PlatformEvent(**_event(f"bulk-ms-{index}", text="x"))],
                    delivery_outcomes_v1=True,
                )
            )["events"][0]["status"] == "routed"

        fences = coordinator.stop_all()

        assert fences == {"s1": 1, "s2": 1, "s3": 1}
        for index, sid in enumerate(("s1", "s2", "s3"), start=1):
            assert await service.reconcile_session(sid, attach_seq=fences[sid]) == [
                f"bulk-ms-{index}"
            ]
            assert service.terminal_outcomes(sid)[f"bulk-ms-{index}"].outcome == "non_deliverable"
        # One audit per session — no session's work was dropped unrecorded.
        non_deliverable = [
            row for kind, row in audit.audits if kind == "event_ingress.non_deliverable"
        ]
        assert len(non_deliverable) == 3
    finally:
        coordinator.stop_all()


@pytest.mark.asyncio
async def test_session_stop_reconciles_routed_work_over_http() -> None:
    """End to end: a routed comment reconciled by the real /stop route."""
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    factory = _case_factory.__wrapped__(patch)
    create_case = await anext(factory)
    try:
        case = await create_case()
        await _assert_stop_reconciles(case, BINDING, p0_event)
    finally:
        await anext(factory, None)  # resume + exhaust the ORIGINAL generator
        patch.undo()


async def _assert_stop_reconciles(case, binding, p0_event) -> None:
    meta = dict(await case.d.store.get(case.sid))
    meta["platform_event_binding"] = binding
    await case.d.store.set(case.sid, meta)

    posted = await case.client.post(
        f"/api/v1/sessions/{case.sid}/events",
        json={
            "events": [p0_event("hi", event_id="e2e-1", source_message_id="msg-e2e").model_dump()],
            "delivery_outcomes_v1": True,
        },
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["events"][0]["status"] == "routed"

    stopped = await case.client.post(f"/api/v1/sessions/{case.sid}/stop")
    assert stopped.status_code == 200, stopped.text

    outcomes = case.d.event_ingestion.terminal_outcomes(case.sid)
    assert outcomes["e2e-1"].outcome == "non_deliverable"


@pytest.mark.asyncio
async def test_comment_the_coordinator_actually_consumed_is_not_non_deliverable() -> None:
    """A routed comment the tick loop consumed must never be reconciled as lost.

    The consumption boundary is ``DirectorCoordinator._tick_once``: the only
    place a comment leaves ``ChatQueue`` into Director state. Without the
    ledger being cleared there, teardown reconciles a genuinely consumed
    comment as ``non_deliverable`` — a false audit record.
    """
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    factory = _case_factory.__wrapped__(patch)
    create_case = await anext(factory)
    try:
        case = await create_case()
        meta = dict(await case.d.store.get(case.sid))
        meta["platform_event_binding"] = BINDING
        await case.d.store.set(case.sid, meta)

        posted = await case.client.post(
            f"/api/v1/sessions/{case.sid}/events",
            json={
                "events": [
                    p0_event("hi", event_id="e2e-2", source_message_id="msg-e2e-2").model_dump()
                ],
                "delivery_outcomes_v1": True,
            },
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["events"][0]["status"] == "routed"

        # The coordinator really drains the queue into Director state.
        case.d.coordinator._activated.add(case.sid)
        await case.d.coordinator._tick_once(case.sid)

        stopped = await case.client.post(f"/api/v1/sessions/{case.sid}/stop")
        assert stopped.status_code == 200, stopped.text

        outcomes = case.d.event_ingestion.terminal_outcomes(case.sid)
        assert outcomes["e2e-2"].outcome == "consumed"
    finally:
        await anext(factory, None)  # resume + exhaust the ORIGINAL generator
        patch.undo()


@pytest.mark.asyncio
async def test_stop_all_returns_the_fence_each_session_needed() -> None:
    """``stop_all`` must hand back what ``stop`` returns, per session.

    The fence is the only thing that lets a caller reconcile; dropping it is
    what made bulk teardown lose routed work silently.
    """
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    factory = _case_factory.__wrapped__(patch)
    create_case = await anext(factory)
    try:
        case = await create_case()
        meta = dict(await case.d.store.get(case.sid))
        meta["platform_event_binding"] = BINDING
        await case.d.store.set(case.sid, meta)
        await case.client.post(
            f"/api/v1/sessions/{case.sid}/events",
            json={
                "events": [
                    p0_event("hi", event_id="fence-1", source_message_id="msg-fence").model_dump()
                ],
                "delivery_outcomes_v1": True,
            },
        )

        fences = case.d.coordinator.stop_all()

        assert fences == {case.sid: 1}
        assert await case.d.event_ingestion.reconcile_session(
            case.sid, attach_seq=fences[case.sid]
        ) == ["fence-1"]
    finally:
        await anext(factory, None)  # resume + exhaust the ORIGINAL generator
        patch.undo()


@pytest.mark.asyncio
async def test_process_shutdown_reconciles_routed_work_instead_of_dropping_it() -> None:
    """A bulk teardown must audit routed work, not lose it.

    ``/stop`` reconciles; the shutdown path (coordinator ``stop_all``) only
    stopped the coordinator, so a comment that was routed but never consumed
    died with the queue and left NO audit row at all — silent loss.
    """
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    factory = _case_factory.__wrapped__(patch)
    create_case = await anext(factory)
    try:
        case = await create_case()
        meta = dict(await case.d.store.get(case.sid))
        meta["platform_event_binding"] = BINDING
        await case.d.store.set(case.sid, meta)

        posted = await case.client.post(
            f"/api/v1/sessions/{case.sid}/events",
            json={
                "events": [
                    p0_event("hi", event_id="bulk-1", source_message_id="msg-bulk").model_dump()
                ],
                "delivery_outcomes_v1": True,
            },
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["events"][0]["status"] == "routed"

        # Process shutdown: every session's coordinator goes away at once.
        await _shutdown(case.d)

        outcomes = case.d.event_ingestion.terminal_outcomes(case.sid)
        assert outcomes["bulk-1"].outcome == "non_deliverable"
    finally:
        await anext(factory, None)  # resume + exhaust the ORIGINAL generator
        patch.undo()


# ---------------------------------------------------------------------------
# Hard expiry — the ORIGINAL ``occurred_at`` is retained, never reset to "now".
#
# ``_reject_reason`` only rejects; nothing in the delivery path is allowed to
# rewrite the timestamp. A reset would turn a hard-expired comment back into a
# fresh one and defeat the 24h staleness bound entirely, so these are the
# regression guards for that specific false-success class.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_expired_rejection_audits_the_original_occurred_at() -> None:
    """A hard-expired event is audited as rejected against the ORIGINAL instant.

    The audit surface is where an operator answers "when did this comment
    actually happen?". Recording the ingestion time instead would make an
    expired comment indistinguishable from a live one after the fact.
    """
    audit = _AuditSink()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, pg_store=audit)
    expired_at = time.time() - 60 * 60 * 24 * 3
    event = PlatformEvent(**_event("exp-1", text="old", occurred_at=expired_at))
    await service.ingest("s1", [event])

    # The rejected event is audited, not just counted.
    rejected = [row for kind, row in audit.audits if kind == "event_ingress.rejected"]
    assert len(rejected) == 1
    assert rejected[0]["resource"] == "viewer.comment:exp-1"
    # Retention on the object the rejection was decided against: had any step
    # stamped a present-tense value onto the event, this would not hold.
    assert event.occurred_at == expired_at
    # Rejected work never enters the delivery ledger.
    assert "exp-1" not in service._in_flight.get("s1", {})


@pytest.mark.asyncio
async def test_hard_expired_event_is_never_retried_as_fresh() -> None:
    """A hard-expired event stays expired no matter how often it is retried.

    This is the bound the requirement defends: a hard-expired comment must not
    come back as a success on a later attempt, which is exactly what a silent
    ``occurred_at = now`` reset would produce.
    """
    now = time.time()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store)
    expired_at = now - 60 * 60 * 24 * 3
    event = PlatformEvent(**_event("exp-2", text="old", occurred_at=expired_at))

    first = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert first["events"][0]["status"] == "rejected"

    # Retry much later, when the original timestamp is even more ancient.
    service._now = lambda: now + 60 * 60 * 24
    retry = await service.ingest("s1", [event], delivery_outcomes_v1=True)

    assert retry["events"][0]["status"] == "rejected"
    assert retry["events"][0]["reason"] == "occurred_at_out_of_range"


@pytest.mark.asyncio
async def test_routed_delivery_path_never_mutates_occurred_at() -> None:
    """Routing, consumption, and reconciliation leave the event untouched.

    The delivery ledger keeps the whole ``PlatformEvent``, and every terminal
    outcome is a ``dataclasses.replace`` that swaps only the ``DeliveryResult``.
    If any of those transitions rebuilt the event with a fresh timestamp, the
    retained provenance would be a lie.
    """
    coordinator = _RoutedTeardownCoordinator()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator)
    occurred_at = time.time() - 120.0
    event = PlatformEvent(**_event("ts-1", text="hi", occurred_at=occurred_at))

    await service.ingest("s1", [event], delivery_outcomes_v1=True)
    in_flight = service._in_flight["s1"]["ts-1"]
    assert in_flight.event.occurred_at == occurred_at

    # The instant handed to the coordinator is the ORIGINAL one. This is the
    # assertion a delivery-path timestamp reset actually breaks: the ledger
    # would still read correctly while the queued comment silently went stale.
    assert coordinator.queued_ts == [occurred_at]
    service.mark_consumed("s1", {"comment-1"})
    consumed = service.terminal_outcomes("s1")["ts-1"]

    # Consumed is terminal and still keyed to the original timestamp.
    assert consumed.outcome == "consumed"
    assert service._outcome_log["s1"]["ts-1"].event.occurred_at == occurred_at

    # The non_deliverable leg mutates the outcome the same way: event untouched.
    second_at = time.time() - 90.0
    await service.ingest(
        "s1",
        [PlatformEvent(**_event("ts-2", text="hi2", occurred_at=second_at))],
        delivery_outcomes_v1=True,
    )
    await service.reconcile_session("s1", attach_seq=coordinator.next_delivery_tick())

    assert coordinator.queued_ts == [occurred_at, second_at]
    reconciled = service._outcome_log["s1"]["ts-2"]
    assert reconciled.delivery.outcome == "non_deliverable"
    assert reconciled.event.occurred_at == second_at


@pytest.mark.asyncio
async def test_not_ready_retry_succeeding_later_keeps_the_original_occurred_at() -> None:
    """A retry that succeeds must not have refreshed the event's timestamp.

    The ``not_ready`` leg leaves nothing behind, so the API re-drives the exact
    same event. If the retry path stamped a fresh ``occurred_at``, a comment
    that was already near the 24h bound would be handed a second, younger
    lifetime and could be delivered long after the platform reported it.
    """
    coordinator = _AttachableCoordinator()
    store = InMemorySessionStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=coordinator)
    now = time.time()
    occurred_at = now - 60 * 60 * 12  # still inside the 24h bound
    event = PlatformEvent(**{**_event("ts-3", text="hi"), "occurred_at": occurred_at})

    first = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert first["events"][0]["status"] == "not_ready"

    # A day later the ORIGINAL timestamp is at the edge of the bound.
    service._now = lambda: now + 60 * 60 * 11
    coordinator.attach()
    second = await service.ingest("s1", [event], delivery_outcomes_v1=True)

    # Retention is what makes this legal: the instant never moved.
    assert second["events"][0]["status"] == "routed"
    assert service._in_flight["s1"]["ts-3"].event.occurred_at == occurred_at
    # The successful retry queued the ORIGINAL instant, not its own "now".
    assert coordinator.queued_ts == [occurred_at]


@pytest.mark.asyncio
async def test_p0_v1_binding_path_preserves_the_original_occurred_at() -> None:
    """The p0.v1 HTTP path must carry the caller's ``occurred_at`` through.

    This is the production ingress: the request body is validated by Pydantic
    and handed to the service untouched. A default or overwrite applied while
    parsing would silently re-date every p0.v1 comment.
    """
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    factory = _case_factory.__wrapped__(patch)
    create_case = await anext(factory)
    try:
        case = await create_case()
        meta = dict(await case.d.store.get(case.sid))
        meta["platform_event_binding"] = BINDING
        await case.d.store.set(case.sid, meta)
        occurred_at = time.time() - 60 * 60 * 2

        posted = await case.client.post(
            f"/api/v1/sessions/{case.sid}/events",
            json={
                "events": [
                    p0_event(
                        "hi", event_id="ts-p0", source_message_id="msg-ts", occurred_at=occurred_at
                    ).model_dump()
                ],
                "delivery_outcomes_v1": True,
            },
        )

        assert posted.status_code == 200, posted.text
        assert posted.json()["events"][0]["status"] == "routed"
        # The event the ledger holds still carries the caller's instant.
        assert case.d.event_ingestion._in_flight[case.sid]["ts-p0"].event.occurred_at == occurred_at
        # ...and so does the comment the coordinator actually queued.
        queued = case.d.coordinator._queues[case.sid].snapshot()
        assert [comment.ts for comment in queued] == [occurred_at]
    finally:
        await anext(factory, None)  # resume + exhaust the ORIGINAL generator
        patch.undo()


@pytest.mark.asyncio
async def test_p0_v1_hard_expired_comment_stays_rejected_over_http() -> None:
    """A p0.v1 comment past the bound is rejected, and stays rejected on retry.

    The p0.v1 validator already refuses ``occurred_at <= 0``, so this proves the
    OTHER half of the requirement: a real but hard-expired instant is preserved
    and rejected rather than defaulted to a present-tense one.
    """
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    factory = _case_factory.__wrapped__(patch)
    create_case = await anext(factory)
    try:
        case = await create_case()
        meta = dict(await case.d.store.get(case.sid))
        meta["platform_event_binding"] = BINDING
        await case.d.store.set(case.sid, meta)
        occurred_at = time.time() - 60 * 60 * 24 * 3

        async def post(event_id: str) -> dict:
            response = await case.client.post(
                f"/api/v1/sessions/{case.sid}/events",
                json={
                    "events": [
                        p0_event(
                            "hi",
                            event_id=event_id,
                            source_message_id=f"msg-{event_id}",
                            occurred_at=occurred_at,
                        ).model_dump()
                    ],
                    "delivery_outcomes_v1": True,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["events"][0]

        assert await post("ts-p0-stale") == {
            "event_id": "ts-p0-stale",
            "status": "rejected",
            "reason": "occurred_at_out_of_range",
        }
        # An immediate retry of the same id is deduped off the recorded
        # rejection — never delivered, and never re-dated into a fresh event.
        retry = await post("ts-p0-stale")
        assert retry["status"] == "duplicate"
        assert retry["status"] not in ("accepted", "routed")
    finally:
        await anext(factory, None)  # resume + exhaust the ORIGINAL generator
        patch.undo()


# ---------------------------------------------------------------------------
# P0-FB-013 negative-test cases 3 and 7 — a coordinator that dies mid-batch.
# Report #61/#62: one event's failure must not discard the truthful outcomes
# the caller already holds for the events processed before it.
# ---------------------------------------------------------------------------


class _DyingMidBatchCoordinator:
    """Routes the first comment, then the coordinator is gone for the second.

    ``ingest`` raising KeyError is what a teardown between the routing check
    and the put looks like from here. The comment routed before the teardown
    is real work that must not be re-driven by the retry.
    """

    def __init__(self) -> None:
        self.ingested: list[tuple[str, str]] = []

    def has(self, session_id: str) -> bool:
        return True

    def ingest(self, session_id, text, author, ts=None):
        if self.ingested:
            raise CoordinatorUnavailable(session_id)
        self.ingested.append((session_id, text))
        return type("C", (), {"id": "comment-1"})()


@pytest.mark.asyncio
async def test_teardown_mid_batch_keeps_earlier_outcomes_and_reports_not_ready() -> None:
    coordinator = _DyingMidBatchCoordinator()
    service, _ = await _fresh_service(coordinator=coordinator)

    result = await service.ingest(
        "s1",
        [
            PlatformEvent(**_event("b-1", text="first")),
            PlatformEvent(**_event("b-2", text="second")),
        ],
        delivery_outcomes_v1=True,
    )
    items = result["events"]

    # The event that was routed before the teardown keeps its true outcome
    # instead of the whole batch being reported as one failure.
    assert items[0]["status"] == "routed"
    assert items[0]["comment_id"] == "comment-1"
    # The event that hit the teardown is retryable and keeps its identity, so
    # the durable retry can re-drive it once a coordinator is attached.
    assert items[1]["status"] == "not_ready"
    assert items[1]["event_id"] == "b-2"
    assert items[1]["reason"] == "coordinator_torn_down_before_acceptance"
    assert items[1]["action_identity"] == "b-2"
    # not_ready is not a rejection and not a delivery.
    assert result["accepted"] == 0
    assert result["rejected"] == 0


@pytest.mark.asyncio
async def test_teardown_mid_batch_does_not_lose_earlier_delivery_on_retry() -> None:
    coordinator = _DyingMidBatchCoordinator()
    service, _ = await _fresh_service(coordinator=coordinator)

    await service.ingest(
        "s1",
        [
            PlatformEvent(**_event("b-1", text="first")),
            PlatformEvent(**_event("b-2", text="second")),
        ],
        delivery_outcomes_v1=True,
    )

    retry = await service.ingest(
        "s1",
        [
            PlatformEvent(**_event("b-1", text="first")),
            PlatformEvent(**_event("b-2", text="second")),
        ],
        delivery_outcomes_v1=True,
    )
    items = retry["events"]

    # Case 7: the dedup write lands after routing, so an interruption in that
    # window can queue the comment a second time. What must never happen is a
    # second *completion*: the already-routed event is recognised as delivered.
    assert items[0]["status"] == "duplicate"
    assert items[0]["status"] != "routed"
    routed_again = [item for item in items if item["status"] == "routed"]
    assert routed_again == [], f"a delivered event was completed twice: {items}"


@pytest.mark.asyncio
async def test_legacy_contract_still_fails_the_batch_on_error() -> None:
    # Without the opt-in there is no retryable vocabulary to report, so the
    # batch must fail loudly rather than invent a not_ready the caller cannot
    # interpret.
    coordinator = _DyingMidBatchCoordinator()
    service, _ = await _fresh_service(coordinator=coordinator)

    with pytest.raises(KeyError):
        await service.ingest(
            "s1",
            [
                PlatformEvent(**_event("b-1", text="first")),
                PlatformEvent(**_event("b-2", text="second")),
            ],
        )


@pytest.mark.asyncio
async def test_real_chat_queue_overflow_is_retryable_never_evicting() -> None:
    """Real coordinator + real ChatQueue pushed past ``max_size`` (P0-FB-013).

    No double: the queue is filled to its real capacity through the real
    ingestion path, then more events arrive. Later events must be explicit
    retryable ``not_ready/queue_full``; nothing queued is evicted/reordered,
    and capacity returns once the tick loop has consumed the queued comments.
    """
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    factory = _case_factory.__wrapped__(patch)
    create_case = await anext(factory)
    try:
        case = await create_case()
        meta = dict(await case.d.store.get(case.sid))
        meta["platform_event_binding"] = BINDING
        await case.d.store.set(case.sid, meta)
        coordinator, ingestion = case.d.coordinator, case.d.event_ingestion
        queue = coordinator._queues[case.sid]
        size = queue.max_size

        def batch(prefix, n):
            return [
                p0_event(
                    f"{prefix}-{i}",
                    event_id=f"{prefix}-{i}",
                    source_message_id=f"msg-{prefix}-{i}",
                )
                for i in range(n)
            ]

        fill = await ingestion.ingest(case.sid, batch("fill", size), delivery_outcomes_v1=True)
        assert [e["status"] for e in fill["events"]] == ["routed"] * size
        queued_ids = [c.id for c in queue.snapshot()]
        assert len(queued_ids) == size

        over = await ingestion.ingest(case.sid, batch("over", 5), delivery_outcomes_v1=True)
        for item in over["events"]:
            assert item["status"] == "not_ready"
            assert item["reason"] == "queue_full"
            assert "comment_id" not in item
        # Nothing evicted, reordered, or reported delivered that was not queued.
        assert [c.id for c in queue.snapshot()] == queued_ids
        assert [c.text for c in queue.snapshot()][0] == "fill-0"
        assert len(queue) == size
        assert {i["comment_id"] for i in fill["events"]} == set(queued_ids)
        assert queue.stats()["received_total"] == size

        # Drain: the tick loop consumes the queued comments; capacity returns.
        coordinator._activated.add(case.sid)
        await coordinator._tick_once(case.sid)
        assert coordinator.queue_capacity(case.sid) > 0
        retry = await ingestion.ingest(case.sid, batch("over", 5), delivery_outcomes_v1=True)
        assert [e["status"] for e in retry["events"]] == ["routed"] * 5
    finally:
        await anext(factory, None)  # resume + exhaust the ORIGINAL generator
        patch.undo()


@pytest.mark.asyncio
async def test_events_posted_to_a_stopped_session_are_not_consumed_or_delivered() -> None:
    """Terminal session (P0-FB-013): a real session is created, stopped, then posted to.

    Pins the CURRENT response: 404 ``unknown session_id``. KNOWN ACCEPTED GAP:
    the label says "unknown" for a stopped session; Main Management decided not
    to change the HTTP label in 013. The API maps the repeated 404 to an audited
    ``non_deliverable`` (``ai_runtime_session_not_found``). What must hold here
    is that nothing is consumed or acknowledged as delivered.
    """
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    factory = _case_factory.__wrapped__(patch)
    create_case = await anext(factory)
    try:
        case = await create_case()
        meta = dict(await case.d.store.get(case.sid))
        meta["platform_event_binding"] = BINDING
        await case.d.store.set(case.sid, meta)
        stopped = await case.client.post(f"/api/v1/sessions/{case.sid}/stop")
        assert stopped.status_code == 200, stopped.text

        posted = await case.client.post(
            f"/api/v1/sessions/{case.sid}/events",
            json={
                "events": [
                    p0_event("late", event_id="late-1", source_message_id="m-late").model_dump()
                ],
                "delivery_outcomes_v1": True,
            },
        )

        assert posted.status_code == 404, posted.text
        assert posted.json() == {"error": {"code": "http_404", "message": "unknown session_id"}}
        assert "events" not in posted.json()  # no per-event result, hence no delivered/routed ack
        assert not case.d.coordinator.has(case.sid)
        assert case.sid not in case.d.coordinator._queues
        assert "late-1" not in case.d.event_ingestion.terminal_outcomes(case.sid)
    finally:
        await anext(factory, None)  # resume + exhaust the ORIGINAL generator
        patch.undo()


class _FencedRejectingStore(InMemorySessionStore):
    """Store whose distributed lock is held but whose commits are rejected."""

    @asynccontextmanager
    async def with_session_lock(self, session_id, acquire_timeout_seconds=None):
        yield object()

    async def commit_if_owner(self, fence, meta) -> bool:
        return False


class _OkCoordinator:
    def has(self, session_id: str) -> bool:
        return True

    def ingest(self, session_id, text, author, ts=None):
        return type("C", (), {"id": "c-ok"})()


@pytest.mark.asyncio
async def test_lost_lock_ownership_is_not_isolated_into_a_fabricated_duplicate() -> None:
    from backend.application.db.session_store import SessionLockTimeout

    store = _FencedRejectingStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=_OkCoordinator())
    event = PlatformEvent(**_event("lost-1", text="hi"))

    # The same event twice: swallowing the stale-owner error would let the
    # second copy hit uncommitted in-memory dedup state and report 'duplicate'.
    with pytest.raises(SessionLockTimeout):
        await service.ingest("s1", [event, event], delivery_outcomes_v1=True)
    # Nothing was persisted, so a later submission is not a duplicate either.
    assert "platform_event_ids" not in (await store.get("s1"))
    with pytest.raises(SessionLockTimeout):
        await service.ingest("s1", [event], delivery_outcomes_v1=True)


class _SecretCoordinator:
    def has(self, session_id: str) -> bool:
        return True

    def ingest(self, session_id, text, author, ts=None):
        raise CoordinatorUnavailable("SECRET-MARKER-123")


@pytest.mark.asyncio
async def test_per_event_failure_log_does_not_leak_exception_text(caplog) -> None:
    service, _ = await _fresh_service(coordinator=_SecretCoordinator())
    with caplog.at_level("DEBUG"):
        result = await service.ingest(
            "s1", [PlatformEvent(**_event("leak-1", text="hi"))], delivery_outcomes_v1=True
        )
    assert result["events"][0]["status"] == "not_ready"
    assert "ingest_event_failed" in caplog.text
    assert "leak-1" in caplog.text and "CoordinatorUnavailable" in caplog.text
    assert "SECRET-MARKER-123" not in caplog.text
    assert "Traceback" not in caplog.text


class _FlakyOnceCoordinator:
    def __init__(self) -> None:
        self.calls = 0

    def has(self, session_id: str) -> bool:
        return True

    def ingest(self, session_id, text, author, ts=None):
        self.calls += 1
        if self.calls == 1:
            raise CoordinatorUnavailable(session_id)
        return type("C", (), {"id": "c-recovered"})()


@pytest.mark.asyncio
async def test_isolated_failure_recovers_on_retry_of_the_same_event() -> None:
    service, _ = await _fresh_service(coordinator=_FlakyOnceCoordinator())
    event = PlatformEvent(**_event("rec-1", text="hi"))
    first = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert first["events"][0]["status"] == "not_ready"
    second = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert second["events"][0]["status"] == "routed"
    assert second["events"][0]["comment_id"] == "c-recovered"
    assert (await service.ingest("s1", [event], delivery_outcomes_v1=True))["events"][0][
        "status"
    ] == "duplicate"


def test_mark_consumed_ignores_evicted_ids_and_stays_bounded() -> None:
    from backend.application.director.comment_buffer import ChatQueue

    queue = ChatQueue("s", max_size=3)
    snapshot = [queue.put(f"t{i}", "a") for i in range(3)]
    for i in range(3, 6):  # legacy producer evicts every snapshot entry
        queue.put(f"t{i}", "a")
    queue.mark_consumed([c.id for c in snapshot] * 2)  # evicted + duplicates
    assert len(queue._consumed) == 0
    live = [c.id for c in queue.snapshot()]
    queue.mark_consumed(live + live)
    assert len(queue._consumed) <= len(queue) == 3
    queue.clear()
    assert len(queue) == 0 and not queue._consumed


@pytest.mark.asyncio
async def test_injected_keyerror_during_dedup_commit_escapes_the_guard(monkeypatch) -> None:
    service, store = await _fresh_service(coordinator=_OkCoordinator())

    async def boom(*args, **kwargs):
        raise KeyError("dedup-commit")

    monkeypatch.setattr(service, "_record_seen", boom)
    event = PlatformEvent(**_event("kd-1", text="hi"))
    # Legacy behaviour: the batch fails; no not_ready followed by a duplicate.
    with pytest.raises(KeyError):
        await service.ingest("s1", [event, event], delivery_outcomes_v1=True)
    assert "platform_event_ids" not in (await store.get("s1"))


@pytest.mark.asyncio
async def test_malformed_dedup_entry_is_not_reported_as_coordinator_teardown() -> None:
    service, store = await _fresh_service(coordinator=_OkCoordinator())
    await store.set("s1", {"status": "active", "platform_event_ids": [{"ts": time.time()}]})
    with pytest.raises(KeyError) as caught:
        await service.ingest(
            "s1", [PlatformEvent(**_event("mal-1", text="hi"))], delivery_outcomes_v1=True
        )
    assert not isinstance(caught.value, CoordinatorUnavailable)


def test_genuine_coordinator_teardown_is_the_isolated_keyerror_subclass() -> None:
    assert issubclass(CoordinatorUnavailable, KeyError)
    coordinator = DirectorCoordinator.__new__(DirectorCoordinator)
    coordinator._queues = {}
    with pytest.raises(CoordinatorUnavailable):
        coordinator.ingest("gone", "x", "a")


class _Boom(Exception):
    pass


SECRET = "SECRET-LOG-MARKER-456"


@pytest.mark.asyncio
async def test_remaining_ingestion_logs_do_not_leak_exception_text(caplog) -> None:
    class Store(InMemorySessionStore):
        async def get(self, session_id):
            raise _Boom(SECRET)

        async def set(self, session_id, data, ttl_seconds=None):
            raise _Boom(SECRET)

    class Coord:
        def has(self, session_id):
            raise _Boom(SECRET)

        def queue_capacity(self, session_id):
            raise _Boom(SECRET)

        def next_delivery_tick(self, session_id):
            raise _Boom(SECRET)

    service = PlatformEventIngestionService(store=Store(), coordinator=Coord())
    with caplog.at_level("DEBUG", logger="backend.application.platform_events.ingestion"):
        assert await service._session_exists("s1") is False  # coordinator.has path
        with pytest.raises(_Boom):
            await service._load_meta("s1")  # meta read path
        await service._save_meta("s1", {})  # meta write path (non-strict)
        assert service._has_queue_capacity("s1") is True  # queue_capacity path
        assert service._next_delivery_seq("s1") == 0  # next_delivery_tick path
    for fragment in (
        "coordinator.has",
        "meta read",
        "meta write",
        "queue_capacity",
        "next_delivery_tick",
    ):
        assert fragment in caplog.text, fragment
    assert SECRET not in caplog.text
    assert "Traceback" not in caplog.text


class _FailingDedupWriteStore(InMemorySessionStore):
    """Real store write fails once the dedup record is in the metadata."""

    async def set(self, session_id, data, ttl_seconds=None):
        if "platform_event_ids" in data:
            raise ConnectionError("dedup write failed")
        await super().set(session_id, data, ttl_seconds)


@pytest.mark.asyncio
async def test_failed_dedup_write_is_not_reported_as_delivered_or_later_duplicate() -> None:
    store = _FailingDedupWriteStore()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=_OkCoordinator())
    event = PlatformEvent(**_event("dw-1", text="hi"))

    # Routed to the coordinator but its dedup record cannot be persisted: the
    # batch must surface a failure so the API redelivers, never a success.
    with pytest.raises(ConnectionError):
        await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert "platform_event_ids" not in (await store.get("s1"))
    # Redelivery sees no dedup record, so it is processed again (at-least-once);
    # it is never fabricated into a 'duplicate'.
    with pytest.raises(ConnectionError):
        await service.ingest("s1", [event], delivery_outcomes_v1=True)


@pytest.mark.asyncio
async def test_lost_ownership_on_dedup_write_is_not_reported_as_delivered() -> None:
    from backend.application.db.session_store import SessionLockTimeout

    class Fenced(_FencedRejectingStore):
        pass

    store = Fenced()
    await store.set("s1", {"status": "active"})
    service = PlatformEventIngestionService(store=store, coordinator=_OkCoordinator())
    event = PlatformEvent(**_event("dw-2", text="hi"))
    for _ in range(2):
        with pytest.raises(SessionLockTimeout):
            await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert "platform_event_ids" not in (await store.get("s1"))


@pytest.mark.asyncio
async def test_successful_dedup_write_is_recorded_and_second_submission_is_duplicate() -> None:
    service, store = await _fresh_service(coordinator=_OkCoordinator())
    event = PlatformEvent(**_event("dw-3", text="hi"))
    first = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert first["events"][0]["status"] == "routed"
    assert [e["event_id"] for e in (await store.get("s1"))["platform_event_ids"]] == ["dw-3"]
    second = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert second["events"][0]["status"] == "duplicate"
