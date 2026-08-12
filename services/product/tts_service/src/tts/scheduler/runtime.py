"""SchedulerRuntime — continuous dynamic micro-batch dispatch and resolution.

One runtime lane per active provider (task 10.1). Today exactly one provider
exists, so the runtime owns one lane; a future multi-provider deployment
extends this by running one ``SchedulerRuntime`` per provider. Dispatch rules
10.2-10.10: native-batch bound, first-arrival coalescing window, immediate
dispatch on fill/backlog/deadline-urgency, batch-size-one without coalescing
on CPU/non-native providers, exactly-once completion resolution, and
cancelled-after-dispatch result discard.

Two deadline notions are deliberately distinct:
- ``dispatch_deadline`` (admission's deadline minus the dispatch margin) is
  the URGENCY trigger (rule 10.8): once passed, the request dispatches early
  instead of waiting out the window.
- the request's own ``deadline_at`` is the SWEEP trigger (rule 10.10): once
  passed, the request can no longer be served and fails deterministically.

The runtime is single-event-loop only: asyncio serializes all access, so no
locks are needed. The dispatcher background task is woken by ``_wake`` on new
submissions/cancellations and by a sweep timeout while a coalescing window is
open.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Hashable, Optional

from tts.config import RuntimeConfig
from tts.providers.base import TTSProvider
from tts.providers.errors import (
    CancelledError,
    DeadlineExceededError,
    ProviderError,
    ProviderInferenceError,
)
from tts.providers.models import AudioResult, ProviderResult, SynthesisRequest
from tts.scheduler.admission import AdmissionController
from tts.scheduler.fairness import FairnessSelector, PendingPopulation
from tts.scheduler.models import PendingRequest, PendingState

logger = logging.getLogger("tts.scheduler.runtime")

# Deadline sweep cadence: how often the dispatcher re-checks the population
# while a coalescing window is open. The admission dispatch margin (3 s) is
# two orders of magnitude above this tick, so 50 ms is ample; window expiry
# still wakes us exactly on time (the timeout is the min of the two).
SWEEP_INTERVAL_SECONDS = 0.05


def _drain_future(future: asyncio.Future) -> None:
    """Consume an unawaited completion exception (explicit cancel path).

    A disconnected caller never awaits its completion future; without this
    callback the event loop would log "exception was never retrieved".
    """
    if not future.cancelled():
        future.exception()


class SchedulerRuntime:
    """Owns the pending population and dispatches coalesced provider batches.

    The provider, admission controller, fairness selector, and clock are all
    injected; the clock is a plain callable so tests drive dispatch with a
    deterministic fake instead of real time.
    """

    def __init__(
        self,
        *,
        population: PendingPopulation,
        admission: AdmissionController,
        selector: FairnessSelector,
        provider: TTSProvider,
        config: RuntimeConfig,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._population = population
        self._admission = admission
        self._selector = selector
        self._provider = provider
        self._config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        caps = provider.capabilities()
        # 10.2/10.9: effective batch bound and coalescing are provider-shaped.
        self._max_batch_size = min(config.max_batch_size, caps.max_batch_size)
        self._coalesce_window = timedelta(milliseconds=config.coalesce_window_ms)
        if not caps.supports_native_batch:
            self._max_batch_size = 1
            self._coalesce_window = timedelta(0)

        self._wake = asyncio.Event()
        self._open_window_at: Optional[datetime] = None
        # Request identity of dispatched members so cancel() can find them
        # once they leave the pending population (10.7/10.10).
        self._in_flight: dict[str, PendingRequest] = {}
        self._counters = {
            "dispatched_batches": 0,
            "dispatched_requests": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "deadline_exceeded": 0,
            "overload": 0,
        }
        # The dispatcher starts lazily on first use: __init__ may run outside
        # an event loop (sync tests, wiring), and a task created here would
        # leak if the runtime is discarded without close().
        self._dispatcher_task: Optional[asyncio.Task] = None

    # ── public API (task 10.1) ────────────────────────────────────────────────
    def now(self) -> datetime:
        """The runtime's clock (injected; deterministic in tests)."""
        return self._clock()

    async def submit(self, request: SynthesisRequest) -> AudioResult:
        """Admit, queue, and await exactly this request's result.

        Admission errors (validation, duplicate id, overload) raise before
        the request enters the population. A caller disconnect cancels only
        this request; siblings are never disturbed (11.5).
        """
        now = self._clock()
        pending = self._admission.try_admit(request, now)
        pending.completion.add_done_callback(_drain_future)
        self._population.push(pending)
        # 10.4: the coalescing window opens at ARRIVAL time — the first
        # request on an idle/empty slot stamps it here, not at the
        # dispatcher's next tick, so the window always expires on schedule.
        if self._open_window_at is None and self._coalesce_window > timedelta(0):
            self._open_window_at = now + self._coalesce_window
        self._start_dispatcher()
        self._wake.set()
        try:
            return await pending.completion
        except asyncio.CancelledError:
            # Caller disconnected: remove a pending request or mark an
            # in-flight one so its result is discarded at resolve time.
            self.cancel(pending.request_id)
            raise

    def cancel(self, request_id: str) -> None:
        """PENDING -> removed now; IN_FLIGHT -> result discarded at resolve."""
        pending = self._in_flight.get(request_id) or self._find_pending(request_id)
        if pending is None:
            return
        self._admission.cancel(pending)
        if pending.state is PendingState.IN_FLIGHT:
            # The static provider batch is never disturbed; only this caller's
            # completion is discarded when the batch resolves. Siblings are
            # untouched (11.5).
            self._counters["cancelled"] += 1
            return
        self._population.remove(pending)
        self._admission.release(pending)
        pending.state = PendingState.CANCELLED
        self._counters["cancelled"] += 1
        self._fail_completion(
            pending, CancelledError(f"request {request_id!r} cancelled before dispatch")
        )
        self._wake.set()

    def pending_depth(self) -> int:
        """Requests waiting for dispatch (not yet in a provider batch)."""
        return len(self._population)

    def active_sessions(self) -> set[str]:
        """Sessions with pending work in this lane."""
        return {key[0] for key in self._population._queues}

    def stats(self) -> dict[str, int]:
        """Snapshot for metrics (cluster 8): depth, in-flight, counters."""
        return {
            "pending_depth": self.pending_depth(),
            "in_flight": len(self._in_flight),
            "active_sessions": len(self.active_sessions()),
            **self._counters,
        }

    async def close(self) -> None:
        """Stop the dispatcher background task."""
        if self._dispatcher_task is None:
            return
        self._dispatcher_task.cancel()
        try:
            await self._dispatcher_task
        except asyncio.CancelledError:
            pass

    # ── dispatcher loop (rules 10.3-10.8) ─────────────────────────────────────
    def _start_dispatcher(self) -> None:
        """Start the background dispatcher on first use (idempotent)."""
        if self._dispatcher_task is None:
            self._dispatcher_task = asyncio.create_task(self._run_dispatcher())

    async def _run_dispatcher(self) -> None:
        while True:
            timeout = self._wake_timeout()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            while True:
                if not await self._dispatch_once():
                    break

    def _wake_timeout(self) -> Optional[float]:
        """Seconds until the next dispatch re-check.

        With an open coalescing window the dispatcher wakes at window expiry
        (bounded by the sweep cadence so deadline urgency is re-evaluated);
        otherwise it sleeps until a submission sets ``_wake``.
        """
        if self._open_window_at is None:
            return None
        remaining = (self._open_window_at - self._clock()).total_seconds()
        return min(max(0.0, remaining), SWEEP_INTERVAL_SECONDS)

    async def _dispatch_once(self) -> bool:
        """Select, group, and dispatch one round of provider batches.

        Returns True when at least one batch was dispatched (the caller
        re-checks for backlog); False when the slot should idle or the
        coalescing window is still open.
        """
        now = self._clock()
        self._expire_overdue(now)
        candidates = self._selector.select_candidates(self._population, self._max_batch_size, now)
        candidates = [candidate for candidate in candidates if not candidate.cancelled]
        if not candidates:
            self._open_window_at = None
            return False

        groups: dict[Hashable, list[PendingRequest]] = defaultdict(list)
        for candidate in candidates:
            groups[self._provider.batch_key(candidate.synthesis_request)].append(candidate)

        if self._coalesce_window > timedelta(0):
            urgent = any(candidate.is_expired(now) for candidate in candidates)
            window_expired = self._open_window_at <= now
            batch_full = len(candidates) >= self._max_batch_size
            if not urgent and not window_expired and not batch_full:
                # Window still open: stay pending until expiry/fill/urgency.
                return False

        # 10.8: urgent requests dispatch first; the rest go largest-group
        # first for the best fill.
        urgent_groups = [
            group
            for group in groups.values()
            if any(candidate.is_expired(now) for candidate in group)
        ]
        rest = [
            group
            for group in groups.values()
            if not any(candidate.is_expired(now) for candidate in group)
        ]
        for group in urgent_groups + sorted(rest, key=len, reverse=True):
            await self._dispatch_group(group)

        # 10.6: backlog keeps the slot hot — the next batch dispatches
        # immediately (window already expired) instead of opening a new window.
        self._open_window_at = now if self._population_has_candidates() else None
        return True

    def _population_has_candidates(self) -> bool:
        return any(
            not pending.cancelled
            for queue in self._population._queues.values()
            for pending in queue
        )

    # ── dispatch + resolve (tasks 10.7/10.10) ─────────────────────────────────
    async def _dispatch_group(self, members: list[PendingRequest]) -> None:
        """Dispatch one immutable provider batch; membership never mutates."""
        for member in members:
            member.state = PendingState.IN_FLIGHT
            self._population.remove(member)
            self._in_flight[member.request_id] = member
        self._counters["dispatched_batches"] += 1
        self._counters["dispatched_requests"] += len(members)
        try:
            results = await self._provider.synthesize_batch(
                [member.synthesis_request for member in members]
            )
        except Exception as exc:
            self._resolve_failure(members, exc)
            return
        if len(results) != len(members):
            self._resolve_failure(
                members,
                ProviderInferenceError(
                    f"provider returned {len(results)} results for "
                    f"{len(members)} requests; refusing to misalign"
                ),
            )
            return
        for member, result in zip(members, results):
            self._resolve_one(member, result)

    def _resolve_one(self, member: PendingRequest, result: ProviderResult) -> None:
        """Resolve one member's completion exactly once (10.10)."""
        self._in_flight.pop(member.request_id, None)
        self._admission.release(member)
        if member.cancelled:
            # Caller already left; this result is discarded and siblings
            # resolve normally (11.5).
            return
        if result.error is not None:
            member.state = PendingState.FAILED
            self._counters["failed"] += 1
            self._fail_completion(member, result.error)
            return
        member.state = PendingState.DONE
        self._counters["completed"] += 1
        if not member.completion.done():
            member.completion.set_result(result)

    def _resolve_failure(self, members: list[PendingRequest], exc: Exception) -> None:
        """Provider batch failure: every member fails deterministically (11.3)."""
        error = exc if isinstance(exc, ProviderError) else ProviderInferenceError(str(exc))
        for member in members:
            self._in_flight.pop(member.request_id, None)
            self._admission.release(member)
            member.state = PendingState.FAILED
            self._counters["failed"] += 1
            if not member.cancelled:
                self._fail_completion(member, error)

    @staticmethod
    def _fail_completion(member: PendingRequest, error: Exception) -> None:
        if not member.completion.done():
            member.completion.set_exception(error)

    # ── deadline sweep (10.10) ────────────────────────────────────────────────
    def _expire_overdue(self, now: datetime) -> None:
        """Remove pending requests whose own deadline has passed.

        Requests are never killed on the (earlier) dispatch deadline — that
        only marks them urgent (10.8) so they dispatch early instead.
        """
        for queue in list(self._population._queues.values()):
            for pending in list(queue):
                deadline = pending.synthesis_request.deadline_at
                if deadline is None or now < deadline:
                    continue
                self._population.remove(pending)
                self._admission.release(pending)
                pending.state = PendingState.FAILED
                self._counters["deadline_exceeded"] += 1
                self._fail_completion(
                    pending,
                    DeadlineExceededError(
                        f"request {pending.request_id!r} missed its dispatch deadline"
                    ),
                )

    # ── helpers ───────────────────────────────────────────────────────────────
    def _find_pending(self, request_id: str) -> Optional[PendingRequest]:
        for queue in self._population._queues.values():
            for pending in queue:
                if pending.request_id == request_id:
                    return pending
        return None
