"""Unit tests for FastReducer (OpenSpec 4.1-4.6).

Deterministic by construction: an injected fake clock (a mutable ``now``
captured by closure) drives all timing, and a recording fake embedder counts
calls and returns deterministic vectors. No real sleeps in reducer logic —
the only wall-clock usage is ``asyncio.wait_for`` as a TEST BOUND for the
idle-no-wake proof.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.application.reducer import AcceptedComment, FastReducer, FastReducerConfig


def _comment(event_id: str, text: str, ts: float, viewer_key: str | None = "v1") -> AcceptedComment:
    return AcceptedComment(
        event_id=event_id,
        comment_id=event_id,
        text=text,
        ts=ts,
        viewer_key=viewer_key,
    )


class _FakeEmbedder:
    """Recording embedder: counts calls, returns a deterministic vector per text."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(texts)), float(i)] for i in range(len(texts))]


def _make_reducer(
    horizon: float = 75.0,
    wait_ms: int = 300,
    max_pending: int = 500,
) -> tuple[FastReducer, _FakeEmbedder, list[float]]:
    now = [1000.0]
    embedder = _FakeEmbedder()
    reducer = FastReducer(
        config=FastReducerConfig(
            microbatch_max_wait_ms=wait_ms,
            rolling_horizon_sec=horizon,
            max_pending=max_pending,
        ),
        embedder=embedder,
        now_fn=lambda: now[0],
    )
    return reducer, embedder, now


@pytest.mark.asyncio
async def test_config_validation_rejects_non_positive_knobs() -> None:
    for kwargs in (
        {"microbatch_max_wait_ms": 0},
        {"rolling_horizon_sec": 0.0},
        {"max_pending": 0},
    ):
        with pytest.raises(ValueError):
            FastReducerConfig(**kwargs).validate_runtime()


@pytest.mark.asyncio
async def test_idle_session_no_polling_never_accumulates_work() -> None:
    reducer, embedder, now = _make_reducer()

    for _ in range(10):
        now[0] += 0.3
        assert reducer.pending_deadline("s1", now[0]) is None
        assert reducer.drain_batch("s1", now[0]) == []

    assert reducer.stats("s1")["pending"] == 0
    assert reducer.stats("s1")["wake_notifications"] == 0
    assert embedder.calls == []


@pytest.mark.asyncio
async def test_burst_coalesces_into_one_embed_call() -> None:
    reducer, embedder, now = _make_reducer()
    t0 = now[0]
    for i in range(5):
        reducer.notify_new_events("s1", _comment(f"c{i}", f"text {i}", t0 + 0.01 * i))
    deadline = reducer.pending_deadline("s1", now[0])

    assert deadline == t0 + 0.3
    now[0] = deadline + 0.001
    batch = await reducer.run_once("s1", now[0])

    assert len(batch) == 5
    assert embedder.calls == [[f"text {i}" for i in range(5)]]
    assert reducer.stats("s1")["embed_calls"] == 1
    assert reducer.pending_deadline("s1", now[0]) is None
    assert reducer.stats("s1")["pending"] == 0


@pytest.mark.asyncio
async def test_same_comment_id_embeds_once_and_revision_embeds_again() -> None:
    reducer, embedder, now = _make_reducer()
    now[0] += 0.4
    reducer.notify_new_events("s1", _comment("c1", "giá bao nhiêu", now[0]))
    await reducer.run_once("s1", now[0])

    now[0] += 0.4
    reducer.notify_new_events("s1", _comment("c1", "giá bao nhiêu", now[0]))
    await reducer.run_once("s1", now[0])

    assert embedder.calls == [["giá bao nhiêu"]]
    assert reducer.stats("s1")["cache_hits"] == 1

    now[0] += 0.4
    reducer.notify_new_events("s1", _comment("c1", "giá bao nhiêu ạ", now[0]))
    await reducer.run_once("s1", now[0])

    assert embedder.calls == [["giá bao nhiêu"], ["giá bao nhiêu ạ"]]
    assert reducer.stats("s1")["cache_hits"] == 1


@pytest.mark.asyncio
async def test_rolling_horizon_expires_old_demand() -> None:
    reducer, _, now = _make_reducer(horizon=5.0)
    t0 = now[0]
    reducer.notify_new_events("s1", _comment("old", "cũ", t0 - 10.0))
    await reducer.run_once("s1", now[0])

    snapshot = reducer.demand_snapshot("s1", now[0])

    assert snapshot == []


@pytest.mark.asyncio
async def test_horizon_is_configurable_and_independent_of_microbatch_wait() -> None:
    reducer, _, now = _make_reducer(horizon=5.0, wait_ms=300)
    t0 = now[0]
    reducer.notify_new_events("s1", _comment("a", "hoạt động", t0 - 4.0))
    await reducer.run_once("s1", now[0])

    active = reducer.demand_snapshot("s1", now[0])
    expired = reducer.demand_snapshot("s1", now[0] + 1.2)

    assert [d["comment_id"] for d in active] == ["a"]
    assert expired == []


@pytest.mark.asyncio
async def test_max_pending_bounds_batch_dropping_oldest() -> None:
    reducer, _, now = _make_reducer(max_pending=3)
    t0 = now[0]
    for i in range(5):
        reducer.notify_new_events("s1", _comment(f"c{i}", f"text {i}", t0 + i))
    now[0] = t0 + 0.4

    batch = await reducer.run_once("s1", now[0])

    assert [c.event_id for c in batch] == ["c2", "c3", "c4"]
    assert reducer.stats("s1")["pending"] == 0


@pytest.mark.asyncio
async def test_wait_for_work_returns_promptly_after_notify_with_expired_deadline() -> None:
    reducer, _, now = _make_reducer()
    t0 = now[0]
    reducer.notify_new_events("s1", _comment("c1", "hi", t0))
    now[0] = t0 + 0.4

    await asyncio.wait_for(reducer.wait_for_work("s1"), timeout=0.1)


@pytest.mark.asyncio
async def test_wait_for_work_blocks_indefinitely_when_idle() -> None:
    reducer, _, _ = _make_reducer()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(reducer.wait_for_work("s1"), timeout=0.1)


@pytest.mark.asyncio
async def test_wait_for_work_returns_at_deadline_without_extra_notify() -> None:
    reducer, _, now = _make_reducer()
    t0 = now[0]
    reducer.notify_new_events("s1", _comment("c1", "hi", t0))
    now[0] = t0 + 0.3
    # deadline == now: the next loop iteration returns immediately.
    await asyncio.wait_for(reducer.wait_for_work("s1"), timeout=0.1)


@pytest.mark.asyncio
async def test_wait_for_work_holds_until_deadline_when_event_preset() -> None:
    """P1-03: an already-set wake event must not cut the coalescing wait short.

    The waiter must sleep the FULL microbatch window from the first pending
    comment, even though ``notify_new_events`` set the wake event before the
    waiter started (the set event satisfies ``wait_for(event.wait())``
    instantly, returning early and processing nearly one-by-one).
    """
    reducer, _, now = _make_reducer(wait_ms=200)
    t0 = now[0]
    reducer.notify_new_events("s1", _comment("c1", "hi", t0))
    waiter = asyncio.create_task(reducer.wait_for_work("s1"))
    await asyncio.sleep(0.02)  # let the waiter clear the stale wake and enter the bounded wait

    now[0] = t0 + 0.1
    await asyncio.sleep(0.02)
    assert not waiter.done(), "waiter returned before the coalescing deadline"

    now[0] = t0 + 0.19
    await asyncio.sleep(0.02)
    assert not waiter.done(), "waiter returned before the coalescing deadline"

    now[0] = t0 + 0.201
    await asyncio.wait_for(waiter, timeout=0.5)


@pytest.mark.asyncio
async def test_wait_for_work_second_notify_does_not_move_deadline() -> None:
    """P1-03: the deadline anchors on the FIRST pending item only."""
    reducer, _, now = _make_reducer(wait_ms=200)
    t0 = now[0]
    reducer.notify_new_events("s1", _comment("c1", "first", t0))
    waiter = asyncio.create_task(reducer.wait_for_work("s1"))
    await asyncio.sleep(0.02)

    now[0] = t0 + 0.1
    reducer.notify_new_events("s1", _comment("c2", "second", now[0]))
    await asyncio.sleep(0.02)
    assert not waiter.done(), "waiter returned before the coalescing deadline"

    deadline = reducer.pending_deadline("s1", now[0])
    assert deadline == t0 + 0.2  # unchanged by the later notify

    now[0] = t0 + 0.201
    await asyncio.wait_for(waiter, timeout=0.5)


@pytest.mark.asyncio
async def test_wait_for_work_batch_holds_both_comments_one_embed_call() -> None:
    """P1-03: coalescing means the drained batch embeds ONCE for both comments."""
    reducer, embedder, now = _make_reducer(wait_ms=200)
    t0 = now[0]
    reducer.notify_new_events("s1", _comment("c1", "first", t0))
    waiter = asyncio.create_task(reducer.wait_for_work("s1"))
    await asyncio.sleep(0.02)

    now[0] = t0 + 0.1
    reducer.notify_new_events("s1", _comment("c2", "second", now[0]))
    await asyncio.sleep(0.02)
    assert not waiter.done(), "waiter returned before the coalescing deadline"

    now[0] = t0 + 0.201
    await asyncio.wait_for(waiter, timeout=0.5)

    batch = await reducer.run_once("s1", now[0])
    assert [c.event_id for c in batch] == ["c1", "c2"]
    assert embedder.calls == [["first", "second"]]
    assert reducer.stats("s1")["embed_calls"] == 1
