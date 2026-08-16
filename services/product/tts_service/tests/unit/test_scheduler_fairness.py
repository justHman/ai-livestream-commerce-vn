"""Fairness selector: DRR within a tier, per-session FIFO, high-before-normal,
aging/starvation protection (Change T tasks 9.1-9.6, tests 9.7/9.8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


from tts.providers.models import Priority, SynthesisRequest
from tts.scheduler.fairness import FairnessConfig, FairnessSelector, PendingPopulation
from tts.scheduler.models import PendingRequest

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)

# 60-minute script at ~5s per chunk ≈ 720 chunks; deep-queue realism without
# unbounded loops.
A_DEPTH = 60


def _request(session_id: str, chunk_seq: int, **overrides) -> SynthesisRequest:
    base = dict(
        request_id=f"{session_id}-{chunk_seq}",
        session_id=session_id,
        utterance_id="utt-0",
        chunk_seq=chunk_seq,
        input_text="Xin chào",
        submitted_at=NOW,
    )
    base.update(overrides)
    return SynthesisRequest(**base)


def _pending(session_id: str, chunk_seq: int, wait_ms: int = 0, **overrides) -> PendingRequest:
    request = _request(session_id, chunk_seq, **overrides)
    return PendingRequest(
        synthesis_request=request,
        admitted_at=NOW - timedelta(milliseconds=wait_ms),
    )


def _fill(population: PendingPopulation, requests: list[PendingRequest]) -> None:
    for request in requests:
        population.push(request)


def _select(selector: FairnessSelector, population: PendingPopulation, limit: int) -> list[str]:
    return [request.request_id for request in selector.select_candidates(population, limit, NOW)]


def _session_ids(population: PendingPopulation) -> list[str]:
    return list({key[0] for key in population._queues})


# ── 9.1: DRR within a tier ───────────────────────────────────────────────────
async def test_deep_session_does_not_monopolize_slots() -> None:
    population = PendingPopulation()
    _fill(population, [_pending("A", i) for i in range(A_DEPTH)])
    _fill(population, [_pending("B", 0), _pending("B", 1), _pending("B", 2)])
    _fill(population, [_pending("C", 0), _pending("C", 1), _pending("C", 2)])
    selected = _select(FairnessSelector(), population, 9)
    assert selected[0] == "A-0"
    assert selected[1] == "B-0"
    assert selected[2] == "C-0"
    assert "B-0" in selected and "C-0" in selected
    assert selected.count("A-0") <= 1


async def test_selection_is_deterministic_for_same_input() -> None:
    population = PendingPopulation()
    _fill(population, [_pending("A", i) for i in range(5)])
    _fill(population, [_pending("B", i) for i in range(5)])
    selector = FairnessSelector()
    assert _select(selector, population, 4) == _select(selector, population, 4)


# ── 9.2: per-session FIFO ────────────────────────────────────────────────────
async def test_session_chunks_selected_in_order() -> None:
    population = PendingPopulation()
    _fill(population, [_pending("A", i) for i in range(5)])
    selected = _select(FairnessSelector(), population, 5)
    assert selected == ["A-0", "A-1", "A-2", "A-3", "A-4"]


async def test_fifo_order_across_rounds_with_other_sessions() -> None:
    population = PendingPopulation()
    _fill(population, [_pending("A", i) for i in range(4)])
    _fill(population, [_pending("B", i) for i in range(4)])
    selected = _select(FairnessSelector(), population, 8)
    assert selected == [
        "A-0",
        "B-0",
        "A-1",
        "B-1",
        "A-2",
        "B-2",
        "A-3",
        "B-3",
    ]


# ── 9.3/9.4: priority tiers ──────────────────────────────────────────────────
async def test_high_selected_before_normal() -> None:
    population = PendingPopulation()
    _fill(population, [_pending("A", 0, priority=Priority.NORMAL)])
    _fill(population, [_pending("B", 0, priority=Priority.HIGH)])
    selected = _select(FairnessSelector(), population, 1)
    assert selected == ["B-0"]


async def test_mixed_backlog_high_drains_first() -> None:
    population = PendingPopulation()
    _fill(population, [_pending("A", 0), _pending("A", 1), _pending("A", 2)])
    _fill(
        population,
        [_pending("B", 0, priority=Priority.HIGH), _pending("B", 1, priority=Priority.HIGH)],
    )
    selected = _select(FairnessSelector(), population, 5)
    assert selected[:2] == ["B-0", "B-1"]
    assert set(selected[2:]) == {"A-0", "A-1", "A-2"}


async def test_selector_never_preempts_batches() -> None:
    """Selector is pure: it only reads the population, never in-flight state."""
    population = PendingPopulation()
    _fill(population, [_pending("A", 0)])
    result = FairnessSelector().select_candidates(population, 1, NOW)
    assert len(result) == 1
    assert _session_ids(population) == ["A"]  # untouched


# ── 9.5/9.6: aging and starvation protection ─────────────────────────────────
async def test_normal_progress_under_sustained_high_with_aging() -> None:
    selector = FairnessSelector(FairnessConfig(aging_threshold_ms=5_000, quantum=8, aging_boost=16))
    population = PendingPopulation()
    _fill(population, [_pending("N", i) for i in range(4)])
    _fill(population, [_pending("H", i, priority=Priority.HIGH) for i in range(64)])
    selected = _select(selector, population, 68)
    assert "N-0" in selected
    assert "H-0" in selected
    normal_index = next(i for i, rid in enumerate(selected) if rid.startswith("N-"))
    assert normal_index >= 64  # high drains first, but normal is still selected


async def test_aged_normal_selected_within_bounded_rounds_under_sustained_high() -> None:
    """P1-06: NORMAL must not starve while >= limit HIGH requests arrive every round.

    One NORMAL request admitted at t=0 ages past the 100 ms threshold; each
    dispatch round adds 4 fresh HIGH requests and selects up to 4. Without a
    cross-tier aging reserve the NORMAL is never selected (HIGH always fills
    the limit) — the review finding this test encodes.
    """
    selector = FairnessSelector(FairnessConfig(aging_threshold_ms=100, quantum=8, aging_boost=16))
    population = PendingPopulation()
    _fill(population, [_pending("N", 0, wait_ms=500)])
    selected_round: Optional[int] = None
    for round_no in range(3):
        _fill(
            population,
            [_pending(f"H{round_no}", i, priority=Priority.HIGH) for i in range(4)],
        )
        now = NOW + timedelta(milliseconds=500)
        selected = selector.select_candidates(population, 4, now)
        if any(request.request_id == "N-0" for request in selected):
            selected_round = round_no
            break
        for request in selected:
            population.remove(request)
    assert selected_round is not None, "aged NORMAL starved across 3 dispatch rounds"


async def test_aged_normal_boosted_in_same_tier() -> None:
    selector = FairnessSelector(FairnessConfig(aging_threshold_ms=5_000, quantum=1, aging_boost=4))
    population = PendingPopulation()
    _fill(population, [_pending("A", i) for i in range(3)])
    _fill(population, [_pending("B", 0, wait_ms=6_000)])
    selected = _select(selector, population, 4)
    assert selected[0] == "B-0"  # aged request jumps ahead in round-robin


async def test_fresh_sessions_share_slots_with_aging() -> None:
    selector = FairnessSelector(FairnessConfig(aging_threshold_ms=5_000, quantum=1, aging_boost=1))
    population = PendingPopulation()
    _fill(population, [_pending("A", i) for i in range(4)])
    _fill(population, [_pending("B", 0, wait_ms=6_000)])
    selected = _select(selector, population, 3)
    assert "B-0" in selected
    assert len({rid.split("-")[0] for rid in selected}) == 2
