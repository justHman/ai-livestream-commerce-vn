"""Deterministic bounded-concurrency scheduler (task 10.2).

A FIFO slot scheduler: queued product workflows are promoted into active
slots at each round, never exceeding ``max_product_concurrency``. Pure and
synchronous — ``promote``/``release`` are explicit calls the batch
orchestrator drives, so tests get full determinism (no threads, no asyncio,
no race dependence). Active windows are recorded per workflow so tests can
assert the configured concurrency bound directly (task 10.9).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Protocol


class Schedulable(Protocol):
    """Minimal scheduler input: anything keyed by a stable product id."""

    product_id: str


@dataclass
class ActiveWindow:
    """One workflow's active window: rounds ``[start_round, end_round]``.

    A workflow is active in round ``r`` when ``start_round <= r <=
    end_round`` — it did semantic work in every round of its window.
    """

    workflow: Schedulable
    start_round: int
    end_round: Optional[int] = None


class BoundedScheduler:
    """FIFO slot scheduler enforcing a product-concurrency bound.

    A workflow occupies one slot from promotion until release; queued
    workflows wait for a free slot, so at no round do more than
    ``max_concurrency`` workflows run semantic work simultaneously
    (Decision 10). Segments inside one product remain sequential — the
    bound is across products, never within.
    """

    def __init__(self, max_concurrency: int = 3) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.max_concurrency = max_concurrency
        self._queued: deque[Schedulable] = deque()
        self._active: dict[str, Schedulable] = {}
        self._windows: list[ActiveWindow] = []
        self._windows_by_id: dict[str, ActiveWindow] = {}
        self._round: int = 0

    @property
    def round(self) -> int:
        """Number of promotion rounds executed so far."""
        return self._round

    @property
    def is_busy(self) -> bool:
        """True while queued or active workflows remain."""
        return bool(self._queued) or bool(self._active)

    def enqueue(self, workflow: Schedulable) -> None:
        """Add a workflow to the waiting queue (FIFO)."""
        self._queued.append(workflow)

    def promote(self) -> list[Schedulable]:
        """Advance one round and fill free slots from the queue.

        Returns the workflows promoted into active slots this round. The
        active set never exceeds ``max_concurrency``; remaining workflows
        stay queued until capacity becomes available.
        """
        self._round += 1
        promoted: list[Schedulable] = []
        while self._queued and len(self._active) < self.max_concurrency:
            workflow = self._queued.popleft()
            self._active[workflow.product_id] = workflow
            window = ActiveWindow(workflow=workflow, start_round=self._round)
            self._windows.append(window)
            self._windows_by_id[workflow.product_id] = window
            promoted.append(workflow)
        return promoted

    def active(self) -> list[Schedulable]:
        """Workflows currently holding slots (deterministic order)."""
        return list(self._active.values())

    def release(self, workflow: Schedulable) -> None:
        """Free a workflow's slot and close its active window."""
        window = self._windows_by_id.pop(workflow.product_id, None)
        if window is not None:
            window.end_round = self._round
        self._active.pop(workflow.product_id, None)

    def drain_queued(self) -> list[Schedulable]:
        """Return and clear all queued workflows (used by cancellation)."""
        items = list(self._queued)
        self._queued.clear()
        return items

    def max_active_overlap(self) -> int:
        """Maximum number of workflows active in any single round.

        Tests assert the configured concurrency bound on this value (task
        10.9): with 20 products and max 3, the result must be <= 3.
        """
        best = 0
        for r in range(1, self._round + 1):
            count = 0
            for window in self._windows:
                if window.start_round <= r and (window.end_round is None or window.end_round >= r):
                    count += 1
            best = max(best, count)
        return best
