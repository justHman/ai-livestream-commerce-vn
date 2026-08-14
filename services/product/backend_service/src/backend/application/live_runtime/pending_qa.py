"""Bounded pending-Q&A candidate board (task 14.4).

Holds the small set of viewer-Q&A cluster candidates the speech arbiter may
interleave between approved script sentences (design Decisions 17/18). The
reducer writes candidates here while a script sentence plays; the arbiter
only READS at the safe sentence boundary, so mid-sentence updates land
without ever preempting the playing sentence.

Boundedness: at most ``max_candidates`` entries (deterministic tie-break by
``first_seen_at``), each candidate carries only the fields the arbiter
consumes — never raw viewer text. Supersession: a newer cluster replaces the
pending winner only when its score exceeds the winner's by
``supersede_ratio`` (score hysteresis), preventing flapping on a temporary
hot candidate.

Thread-safety: the board is a plain in-process object; the reducer writes
through ``update()`` (sync) and the arbiter reads at boundaries. The arbiter
loop drains reducer output via ``notify_new_candidates`` at safe points, so
no cross-thread mutation is shared without the caller's own synchronization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "PendingQaCandidate",
    "PendingQaStore",
    "QaHysteresisConfig",
]


@dataclass(frozen=True, slots=True)
class QaHysteresisConfig:
    """Thresholds revalidated at the safe sentence boundary (tasks 14.4/14.5)."""

    max_candidates: int = 3
    supersede_ratio: float = 1.2
    min_eligibility_score: float = 0.4
    relevance_window: float = 75.0
    cooldown_after_answer: float = 30.0


@dataclass(frozen=True, slots=True)
class PendingQaCandidate:
    """One bounded Q&A candidate (cluster envelope view + score/recency).

    ``envelope`` is the opaque decision-9-shaped cluster envelope the arbiter
    hands to the resolver; only its cluster id is telemetry-exposed.
    """

    cluster_id: str
    envelope: Any
    score: float
    first_seen_at: float
    last_seen_at: float


class PendingQaStore:
    """Bounded, deterministic pending-Q&A candidate board.

    Upsert by ``cluster_id``: a refreshed cluster keeps its identity and
    updates score/recency; at capacity the lowest-score candidate is dropped
    (ties broken by ``first_seen_at``, oldest first). ``pending_winner``
    applies supersession hysteresis; ``is_eligible`` applies cooldown +
    activeness.
    """

    def __init__(
        self,
        config: QaHysteresisConfig | None = None,
        now: Any | None = None,
    ) -> None:
        self._config = config or QaHysteresisConfig()
        self._now = now or (lambda: 0.0)
        self._candidates: dict[str, PendingQaCandidate] = {}
        self._cooldown_until: dict[str, float] = {}

    @property
    def config(self) -> QaHysteresisConfig:
        return self._config

    def update(self, envelope: Any, now: float | None = None) -> PendingQaCandidate | None:
        """Upsert one cluster envelope; return the stored candidate.

        A newer cluster may replace the pending winner on the next
        ``pending_winner`` call; here it just enters the bounded board.
        Returns None when the board is full and the new candidate is the
        evicted one.
        """
        now = self._now() if now is None else now
        cluster_id = envelope.cluster_id
        score = float(getattr(envelope, "ranking_score", 0.0))
        existing = self._candidates.get(cluster_id)
        if existing is not None:
            candidate = PendingQaCandidate(
                cluster_id=cluster_id,
                envelope=envelope,
                score=score,
                first_seen_at=existing.first_seen_at,
                last_seen_at=now,
            )
        else:
            candidate = PendingQaCandidate(
                cluster_id=cluster_id,
                envelope=envelope,
                score=score,
                first_seen_at=now,
                last_seen_at=now,
            )
        if (
            len(self._candidates) >= self._config.max_candidates
            and cluster_id not in self._candidates
        ):
            victim_id = min(
                self._candidates,
                key=lambda key: (
                    self._candidates[key].score,
                    self._candidates[key].first_seen_at,
                ),
            )
            victim = self._candidates[victim_id]
            if (candidate.score, candidate.first_seen_at) < (victim.score, victim.first_seen_at):
                return None
            del self._candidates[victim_id]
            self._cooldown_until.pop(victim_id, None)
        self._candidates[cluster_id] = candidate
        return candidate

    def pending_winner(self, now: float | None = None) -> PendingQaCandidate | None:
        """Highest-scoring ELIGIBLE candidate (supersession hysteresis).

        Boundary revalidation (design Decision 18) lives here: candidates in
        cooldown or outside the relevance window cannot win. A newer cluster
        (later ``first_seen_at``) replaces the current highest-scoring one
        only when its score exceeds it by ``supersede_ratio``. Below
        ``min_eligibility_score`` there is no winner.
        """
        now = self._now() if now is None else now
        eligible = [
            candidate for candidate in self._candidates.values() if self.is_eligible(candidate, now)
        ]
        if not eligible:
            return None
        best = max(eligible, key=lambda candidate: candidate.score)
        if best.score < self._config.min_eligibility_score:
            return None
        for candidate in eligible:
            if (
                candidate.first_seen_at > best.first_seen_at
                and candidate.score > best.score * self._config.supersede_ratio
            ):
                best = candidate
        return best

    def active_candidates(self, now: float | None = None) -> list[PendingQaCandidate]:
        """Candidates still active in the rolling relevance window."""
        now = self._now() if now is None else now
        return [
            candidate
            for candidate in self._candidates.values()
            if now - candidate.last_seen_at <= self._config.relevance_window
        ]

    def is_eligible(self, candidate: PendingQaCandidate, now: float | None = None) -> bool:
        """Activeness revalidation (task 14.5): within window and out of cooldown."""
        now = self._now() if now is None else now
        if now - candidate.last_seen_at > self._config.relevance_window:
            return False
        return now >= self._cooldown_until.get(candidate.cluster_id, 0.0)

    def drop(self, cluster_id: str) -> None:
        self._candidates.pop(cluster_id, None)
        self._cooldown_until.pop(cluster_id, None)

    def mark_answered(self, cluster_id: str, now: float | None = None) -> None:
        """Put a cluster into cooldown; it stays answerable again later."""
        now = self._now() if now is None else now
        self._cooldown_until[cluster_id] = now + self._config.cooldown_after_answer

    def clear(self) -> None:
        self._candidates.clear()
        self._cooldown_until.clear()

    def as_dict(self) -> dict[str, object]:
        """Content-safe telemetry: cluster ids, scores, counts — never viewer text."""
        return {
            "candidate_count": len(self._candidates),
            "max_candidates": self._config.max_candidates,
            "candidates": [
                {
                    "cluster_id": candidate.cluster_id,
                    "score": candidate.score,
                    "first_seen_at": candidate.first_seen_at,
                    "last_seen_at": candidate.last_seen_at,
                }
                for candidate in self._candidates.values()
            ],
            "cooldown_cluster_ids": sorted(self._cooldown_until),
        }
