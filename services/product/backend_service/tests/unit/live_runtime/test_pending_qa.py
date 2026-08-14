"""Task 14.4: bounded pending-Q&A candidates with supersession hysteresis.

Proves: supersession (a newer higher-score cluster replaces the winner only
when the score ratio exceeds the configured hysteresis; a lower score never
does), bounded capacity with deterministic tie-break, cooldown eligibility,
activeness revalidation, and content-safe telemetry (never raw viewer text).
"""

from __future__ import annotations

from backend.application.live_runtime.pending_qa import PendingQaStore, QaHysteresisConfig


class FakeEnvelope:
    """Minimal decision-9-shaped envelope; only cluster_id/ranking_score matter."""

    def __init__(self, cluster_id: str, score: float) -> None:
        self.cluster_id = cluster_id
        self.ranking_score = score


class Clock:
    """Injectable monotonic clock for deterministic boundary math."""

    def __init__(self) -> None:
        self.value = 1_000.0

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def __call__(self) -> float:
        return self.value


def _store() -> tuple[PendingQaStore, Clock]:
    clock = Clock()
    return PendingQaStore(config=QaHysteresisConfig(), now=clock), clock


def test_new_candidate_below_min_eligibility_has_no_winner() -> None:
    store, _ = _store()
    store.update(FakeEnvelope("cl-a", 0.3))

    assert store.pending_winner() is None


def test_winner_is_highest_score_candidate() -> None:
    store, _ = _store()
    store.update(FakeEnvelope("cl-a", 0.5))
    store.update(FakeEnvelope("cl-b", 0.9))

    winner = store.pending_winner()
    assert winner is not None
    assert winner.cluster_id == "cl-b"


def test_newer_higher_score_replaces_winner_above_hysteresis() -> None:
    store, _ = _store()
    store.update(FakeEnvelope("cl-old", 0.8))
    store.update(FakeEnvelope("cl-new", 1.1))

    winner = store.pending_winner()
    assert winner is not None
    assert winner.cluster_id == "cl-new"


def test_newer_lower_score_does_not_replace_winner() -> None:
    store, _ = _store()
    store.update(FakeEnvelope("cl-old", 0.9))
    store.update(FakeEnvelope("cl-new", 0.8))

    winner = store.pending_winner()
    assert winner is not None
    assert winner.cluster_id == "cl-old"


def test_update_same_cluster_refreshes_score_and_recency() -> None:
    store, _ = _store()
    store.update(FakeEnvelope("cl-a", 0.5))
    store.update(FakeEnvelope("cl-a", 0.95))

    winner = store.pending_winner()
    assert winner is not None
    assert winner.cluster_id == "cl-a"
    assert winner.score == 0.95


def test_capacity_drops_lowest_score_candidate() -> None:
    store = PendingQaStore(config=QaHysteresisConfig(max_candidates=2), now=Clock())
    store.update(FakeEnvelope("cl-a", 0.5))
    store.update(FakeEnvelope("cl-b", 0.9))
    store.update(FakeEnvelope("cl-c", 0.7))

    assert {c.cluster_id for c in store.active_candidates()} == {"cl-b", "cl-c"}
    assert store.as_dict()["candidate_count"] == 2


def test_tie_break_drops_oldest_candidate_at_capacity() -> None:
    store = PendingQaStore(config=QaHysteresisConfig(max_candidates=2), now=Clock())
    store.update(FakeEnvelope("cl-a", 0.5))
    store.update(FakeEnvelope("cl-b", 0.5))
    store.update(FakeEnvelope("cl-c", 0.5))

    assert "cl-a" not in {c.cluster_id for c in store.active_candidates()}


def test_cooldown_makes_candidate_ineligible() -> None:
    store, clock = _store()
    store.update(FakeEnvelope("cl-a", 0.9))
    winner = store.pending_winner()
    assert winner is not None
    store.mark_answered("cl-a")
    clock.advance(1.0)

    assert store.is_eligible(winner) is False


def test_cooldown_expires_and_candidate_eligible_again() -> None:
    store, clock = _store()
    store.update(FakeEnvelope("cl-a", 0.9))
    winner = store.pending_winner()
    assert winner is not None
    store.mark_answered("cl-a")
    clock.advance(store.config.cooldown_after_answer + 1.0)

    assert store.is_eligible(winner) is True


def test_candidate_outside_relevance_window_is_inactive() -> None:
    store, clock = _store()
    store.update(FakeEnvelope("cl-a", 0.9))
    clock.advance(store.config.relevance_window + 1.0)

    assert store.active_candidates() == []


def test_candidate_inside_relevance_window_is_active() -> None:
    store, _ = _store()
    store.update(FakeEnvelope("cl-a", 0.9))

    assert [c.cluster_id for c in store.active_candidates()] == ["cl-a"]


def test_pending_winner_respects_activeness_at_boundary() -> None:
    store, clock = _store()
    store.update(FakeEnvelope("cl-a", 0.9))
    clock.advance(store.config.relevance_window + 1.0)

    assert store.pending_winner() is None


def test_answered_cluster_enters_cooldown_and_cannot_win() -> None:
    store, clock = _store()
    store.update(FakeEnvelope("cl-a", 0.9))
    store.mark_answered("cl-a")
    clock.advance(1.0)

    assert store.pending_winner() is None


def test_as_dict_never_contains_raw_viewer_text() -> None:
    store, _ = _store()
    store.update(FakeEnvelope("cl-a", 0.9))
    store.mark_answered("cl-a")

    dumped = str(store.as_dict())
    assert "viewer" not in dumped.lower()
    assert "message" not in dumped.lower()
    assert "câu" not in dumped
    assert "giá" not in dumped


def test_bounded_under_thousands_of_candidates() -> None:
    store, _ = _store()
    for index in range(5_000):
        store.update(FakeEnvelope(f"cl-{index % 97}", float(index % 97) / 100))

    assert len(store.active_candidates()) <= store.config.max_candidates
    assert len(store._candidates) <= store.config.max_candidates


def test_drop_removes_candidate_and_cooldown() -> None:
    store, _ = _store()
    store.update(FakeEnvelope("cl-a", 0.9))
    store.mark_answered("cl-a")
    store.drop("cl-a")

    assert store.pending_winner() is None
    assert store.as_dict()["candidate_count"] == 0
