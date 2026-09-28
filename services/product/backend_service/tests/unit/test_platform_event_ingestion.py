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
    assert "pending_platform_chat" not in (await store.get("s1"))


from .test_approved_speech_active import case_factory as _case_factory  # noqa: E402

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

    def attach(self) -> None:
        self.attached = True

    def has(self, session_id: str) -> bool:
        return self.attached

    def ingest(self, session_id, text, author, ts=None):
        self.ingested.append(text)
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
    assert (await store.get("s1"))["pending_platform_chat"][0]["event_id"] == "legacy-1"
    assert (await service.ingest("s1", [event]))["events"][0]["status"] == "duplicate"


# ---------------------------------------------------------------------------
# P0-FB-013 Task 2 — routed work that dies with the coordinator is reconciled
# as audited non_deliverable instead of vanishing into the dropped queue.
# ---------------------------------------------------------------------------


class _RoutedTeardownCoordinator:
    """Coordinator that routes, then can tear down before consuming."""

    def __init__(self) -> None:
        self.attached = True
        self._monotonic = 0
        self.ingested: list[str] = []

    def has(self, session_id: str) -> bool:
        return self.attached

    def ingest(self, session_id, text, author, ts=None):
        self._monotonic += 1
        self.ingested.append(text)
        return type("C", (), {"id": f"comment-{self._monotonic}"})()

    def next_delivery_tick(self) -> int:
        """Monotonic counter the reconciliation compares outcomes against."""
        return self._monotonic


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
    # The coordinator drained the queue before the teardown.
    service.mark_consumed("s1", "td-3")

    assert await service.reconcile_session("s1", attach_seq=0) == []


@pytest.mark.asyncio
async def test_session_stop_reconciles_routed_work_over_http() -> None:
    """End to end: a routed comment reconciled by the real /stop route."""
    from .test_p0_comment_contract import BINDING, event as p0_event

    patch = pytest.MonkeyPatch()
    create_case = await anext(_case_factory.__wrapped__(patch))
    try:
        case = await create_case()
        await _assert_stop_reconciles(case, BINDING, p0_event)
    finally:
        await anext(_case_factory.__wrapped__(patch), None)  # fixture teardown
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
