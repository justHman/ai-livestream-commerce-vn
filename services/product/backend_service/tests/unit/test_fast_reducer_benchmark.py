"""Deterministic fast-lane latency benchmark (OpenSpec 4.5).

In-process, no network, fixed synthetic inputs: 3 bursts of 5 comments. The
clock is fake, so latency equals the injected-clock delta exactly — this is a
correctness test of the fast-lane contract, not a wall-clock measurement. The
harness ranks the demand snapshot by (unique-viewer count desc, recency) and
the report carries the 4.5 metric list.
"""

from __future__ import annotations

import pytest

from backend.application.reducer import AcceptedComment, FastReducer, FastReducerConfig

BURSTS = 3
COMMENTS_PER_BURST = 5


class _Clock:
    """Mutable fake clock: advancing it moves the reducer's view of time."""

    def __init__(self, start: float = 5000.0) -> None:
        self.value = start

    def now(self) -> float:
        return self.value


class _FakeEmbedder:
    """Recording embedder: deterministic vector per text, counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(texts)), float(i)] for i in range(len(texts))]


def _ranked_demand(snapshot: list[dict]) -> list[dict]:
    """Deterministic harness-side ranking (C6 owns the real ranker).

    Primary: unique-viewer count desc; tie-break: recency desc (newer ts
    first). Stable for identical keys.
    """
    return sorted(
        snapshot,
        key=lambda d: (len({d["viewer_key"]}), d["ts"]),
        reverse=True,
    )


async def _run_bursts() -> dict:
    clock = _Clock()
    embedder = _FakeEmbedder()
    reducer = FastReducer(
        config=FastReducerConfig(microbatch_max_wait_ms=300, rolling_horizon_sec=75.0),
        embedder=embedder,
        now_fn=clock.now,
    )
    wait_ms = reducer._config.microbatch_max_wait_ms
    all_ids = set()
    t0 = clock.now()
    for burst in range(BURSTS):
        for i in range(COMMENTS_PER_BURST):
            event_id = f"burst-{burst}-c{i}"
            all_ids.add(event_id)
            reducer.notify_new_events(
                "s1",
                AcceptedComment(
                    event_id=event_id,
                    comment_id=event_id,
                    text=f"comment {burst} {i}",
                    ts=clock.now(),
                    viewer_key=f"viewer-{burst}-{i}",
                ),
            )
        clock.value += wait_ms / 1000.0
        await reducer.run_once("s1", clock.now())
    latency = clock.now() - t0
    snapshot = reducer.demand_snapshot("s1", clock.now())
    ranked = _ranked_demand(snapshot)
    return {
        "latency": latency,
        "latency_ms": latency * 1000.0,
        "accepted_count": len(all_ids),
        "embed_calls": embedder.calls,
        "ranked_demand": ranked,
        "snapshot_ids": {d["comment_id"] for d in snapshot},
        "all_ids": all_ids,
    }


async def test_fast_lane_benchmark_deterministic() -> None:
    report = await _run_bursts()

    assert report["latency"] == pytest.approx(BURSTS * 0.3, abs=1e-9)
    assert report["accepted_count"] == BURSTS * COMMENTS_PER_BURST
    assert report["embed_calls"] == BURSTS
    assert report["snapshot_ids"] == report["all_ids"]
    assert report["ranked_demand"] == sorted(
        report["ranked_demand"],
        key=lambda d: (len({d["viewer_key"]}), d["ts"]),
        reverse=True,
    )
    assert "accepted_count" in report
    assert "embed_calls" in report
    assert "latency_ms" in report
    assert "ranked_demand" in report
