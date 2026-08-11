"""Session-aware fair selection: per-session FIFO + deficit round robin,
two priority tiers (high before normal), aging protection (Change T tasks
9.1-9.6).

``PendingPopulation`` is the state holder (per-session FIFO deques keyed by
priority); ``FairnessSelector`` is the policy that reads it. Selection is a
pure function of population + clock, so the same input always selects the
same candidates — deterministic by construction.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from tts.providers.models import Priority
from tts.scheduler.models import PendingRequest


@dataclass
class FairnessConfig:
    """Selection policy knobs (decided by the runtime from service config).

    ``aging_threshold_ms`` is the wait time that promotes a request into the
    aging queue; ``quantum`` is the DRR credit per round; the aging boost is
    the extra credit an aged session gets per selection round.
    """

    aging_threshold_ms: int = 5_000
    quantum: int = 8
    aging_boost: int = 16


class PendingPopulation:
    """Per-session FIFO pending storage for one runtime lane.

    Order matters: within a session+priority, requests are selected in the
    order they were pushed (chunk order). Removing a request is O(n); n stays
    small because the runtime drains by selection and prunes when requests
    complete/cancel/expire.
    """

    def __init__(self) -> None:
        # deque per (session_id, priority) — FIFO per session per tier.
        self._queues: dict[tuple[str, Priority], deque[PendingRequest]] = defaultdict(deque)

    def push(self, request: PendingRequest) -> None:
        self._queues[(request.session_id, request.synthesis_request.priority)].append(request)

    def remove(self, request: PendingRequest) -> None:
        key = (request.session_id, request.synthesis_request.priority)
        queue = self._queues.get(key)
        if queue is None:
            return
        queue.remove(request)
        if not queue:
            del self._queues[key]

    def has_work(self, session_id: str, priority: Priority) -> bool:
        return bool(self._queues.get((session_id, priority)))

    def session_priorities(self, session_id: str) -> tuple[Priority, ...]:
        return tuple(priority for (sid, priority) in self._queues if sid == session_id)

    def peek(self, session_id: str, priority: Priority) -> Optional[PendingRequest]:
        queue = self._queues.get((session_id, priority))
        return queue[0] if queue else None

    def pop(self, session_id: str, priority: Priority) -> Optional[PendingRequest]:
        key = (session_id, priority)
        queue = self._queues.get(key)
        if not queue:
            return None
        request = queue.popleft()
        if not queue:
            del self._queues[key]
        return request

    def __len__(self) -> int:
        return sum(len(queue) for queue in self._queues.values())


class FairnessSelector:
    """Selects up to ``limit`` eligible pending requests for one dispatch.

    Contract: the returned list is ordered by selection; the runtime groups
    candidates by provider batch key. The selector never mutates the
    population.
    """

    def __init__(self, config: Optional[FairnessConfig] = None) -> None:
        self.config = config or FairnessConfig()

    def select_candidates(
        self,
        population: PendingPopulation,
        limit: int,
        now: datetime,
    ) -> list[PendingRequest]:
        selected: list[PendingRequest] = []
        if limit <= 0:
            return selected
        eligible = self._eligible(population, now)
        for priority in (Priority.HIGH, Priority.NORMAL):
            if len(selected) >= limit:
                break
            selected.extend(self._select_tier(eligible, priority, limit - len(selected)))
        return selected

    # ── tier selection (DRR) ─────────────────────────────────────────────────
    def _select_tier(
        self,
        eligible: dict[tuple[str, Priority], "_SessionQueue"],
        priority: Priority,
        limit: int,
    ) -> list[PendingRequest]:
        """Deficit round robin over the tier's sessions, FIFO per session."""
        selected: list[PendingRequest] = []
        tier = {key: queue for key, queue in eligible.items() if key[1] is priority}
        if not tier:
            return selected
        deficits = {session: 0 for session in {key[0] for key in tier}}
        active = set(deficits)
        rounds = 0
        while len(selected) < limit and active:
            # Aged sessions are visited first in each round so their requests
            # jump ahead of the round-robin order (task 9.5/9.6); session id
            # breaks ties so the output stays deterministic.
            candidates = sorted(
                ((key, tier[key]) for key in tier if key[0] in active),
                key=lambda item: (0 if item[1].has_aged() else 1, item[0][0]),
            )
            if all(deficits[key[0]] <= 0 for key, _ in candidates):
                for session in active:
                    deficits[session] += self._round_credit(session, eligible, priority)
                rounds += 1
                if rounds > 10_000:
                    # ponytail: hard bound on a pathological active-set cycle;
                    # real loads are far below this.
                    break
            for key, queue in candidates:
                session = key[0]
                if deficits[session] <= 0:
                    continue
                request = queue.pop_next()
                if request is None:
                    active.discard(session)  # drained this tier
                    continue
                selected.append(request)
                deficits[session] -= 1
                if len(selected) >= limit:
                    break
        return selected

    def _round_credit(
        self,
        session: str,
        eligible: dict[tuple[str, Priority], "_SessionQueue"],
        priority: Priority,
    ) -> int:
        """Credit per round; sessions with aged work get the boost (9.5/9.6)."""
        queue = eligible.get((session, priority))
        if queue is not None and queue.has_aged():
            return self.config.aging_boost
        return self.config.quantum

    # ── eligibility (aging) ──────────────────────────────────────────────────
    def _eligible(
        self, population: PendingPopulation, now: datetime
    ) -> dict[tuple[str, Priority], "_SessionQueue"]:
        """Snapshot pending work, promoting aged requests to the aging queue.

        A request waiting >= ``aging_threshold_ms`` is moved to the aging
        queue for its session+tier; aged requests are served before regular
        ones of the same session+tier. Deque entries are never mutated.
        """
        result: dict[tuple[str, Priority], _SessionQueue] = {}
        seen: dict[tuple[str, Priority], list[PendingRequest]] = defaultdict(list)
        for (session_id, priority), queue in population._queues.items():
            key = (session_id, priority)
            for request in queue:
                seen[key].append(request)
        for key, requests in seen.items():
            aged = [
                request
                for request in requests
                if _wait_ms(request, now) >= self.config.aging_threshold_ms
            ]
            regular = [
                request
                for request in requests
                if _wait_ms(request, now) < self.config.aging_threshold_ms
            ]
            result[key] = _SessionQueue(aged=aged, regular=regular)
        return result


@dataclass
class _SessionQueue:
    """Ordered request view for one session+tier: aged first, then regular."""

    aged: list[PendingRequest]
    regular: list[PendingRequest]

    def pop_next(self) -> Optional[PendingRequest]:
        if self.aged:
            return self.aged.pop(0)
        if self.regular:
            return self.regular.pop(0)
        return None

    def has_aged(self) -> bool:
        return bool(self.aged)


def _wait_ms(request: PendingRequest, now: datetime) -> float:
    """Milliseconds since admission (0 if admission clock is missing)."""
    if request.admitted_at is None:
        return 0.0
    return (now - request.admitted_at).total_seconds() * 1000.0
