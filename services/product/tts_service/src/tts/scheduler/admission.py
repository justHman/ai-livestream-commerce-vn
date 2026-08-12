"""Scheduler admission accounting and cancellation (Change T tasks 8.2-8.6).

The admission controller owns ACCOUNTING only — it never holds the queue.
Runtime keeps the ``PendingPopulation``; admission tells it whether a request
may enter, and releases capacity when a request leaves. Pre-validation
(profile/style/format capability checks) runs BEFORE capacity is consumed, so
a 4xx rejection never eats a 429 slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from tts.providers.errors import (
    DeadlineExceededError,
    OverloadError,
)
from tts.providers.models import SynthesisRequest
from tts.scheduler.models import PendingRequest


@dataclass(frozen=True)
class DispatchMargin:
    """How early the effective dispatch deadline precedes the request deadline."""

    seconds: float = 3.0


PreValidator = Callable[[SynthesisRequest], None]


class AdmissionController:
    """Bounded global + per-session pending accounting.

    Not thread-safe; the runtime serializes admission. ``validate`` is an
    injected callable that raises a ``ProviderError`` (4xx) when the request
    cannot be served; it runs before capacity is consumed.
    """

    def __init__(
        self,
        global_pending_limit: int,
        per_session_pending_limit: int,
        *,
        validate: Optional[PreValidator] = None,
        request_deadline_ms: int = 30_000,
        dispatch_margin: DispatchMargin = DispatchMargin(),
    ) -> None:
        self.global_pending_limit = global_pending_limit
        self.per_session_pending_limit = per_session_pending_limit
        self.validate = validate
        self.request_deadline_ms = request_deadline_ms
        self.dispatch_margin = dispatch_margin
        self._global_pending = 0
        self._session_pending: dict[str, int] = {}
        self._request_ids: set[str] = set()

    # ── accounting ──────────────────────────────────────────────────────────
    @property
    def global_pending(self) -> int:
        return self._global_pending

    def session_pending(self, session_id: str) -> int:
        return self._session_pending.get(session_id, 0)

    def try_admit(self, request: SynthesisRequest, now: datetime) -> Optional[PendingRequest]:
        """Admit one request, or return None (no error: not over a limit).

        Returns a ready-to-push ``PendingRequest`` on success. The caller
        (runtime) pushes it into the pending population. Pre-validation
        raises a 4xx ``ProviderError`` before any capacity is consumed;
        overload raises 429. ``now`` is the caller's clock.
        """
        if self.validate is not None:
            self.validate(request)

        if request.request_id in self._request_ids:
            raise OverloadError(f"duplicate request_id {request.request_id!r} already accepted")
        if self._global_pending >= self.global_pending_limit:
            raise OverloadError(f"global pending limit {self.global_pending_limit} reached")
        if self._session_pending.get(request.session_id, 0) >= self.per_session_pending_limit:
            raise OverloadError(
                f"session {request.session_id!r} pending limit "
                f"{self.per_session_pending_limit} reached"
            )

        admitted = PendingRequest(
            synthesis_request=request,
            admitted_at=now,
            dispatch_deadline=self._effective_deadline(request, now),
        )
        self._global_pending += 1
        self._session_pending[request.session_id] = (
            self._session_pending.get(request.session_id, 0) + 1
        )
        self._request_ids.add(request.request_id)
        return admitted

    def release(self, request: PendingRequest) -> None:
        """Return capacity held by a request that left the pending population.

        Idempotent per request identity: a released request holds no capacity,
        and re-releasing it must not double-free the accounting.
        """
        if request.request_id not in self._request_ids:
            return
        self._request_ids.discard(request.request_id)
        self._global_pending = max(0, self._global_pending - 1)
        session_id = request.session_id
        remaining = self._session_pending.get(session_id, 0) - 1
        if remaining <= 0:
            # Clean up empty session keys so session_pending() reports 0 again.
            self._session_pending.pop(session_id, None)
        else:
            self._session_pending[session_id] = remaining

    # ── cancellation (task 8.6) ──────────────────────────────────────────────
    def cancel(self, request: PendingRequest) -> None:
        """Mark a request cancelled.

        PENDING → CANCELLED: the request is skipped by selection and the
        runtime removes it. IN_FLIGHT: only the flag flips; the running
        provider batch is never disturbed and the result is discarded later.
        Sibling requests in the same batch are untouched.
        """
        request.cancelled = True

    # ── helpers ──────────────────────────────────────────────────────────────
    def _effective_deadline(self, request: SynthesisRequest, now: datetime) -> datetime:
        deadline = request.deadline_at
        if deadline is None:
            deadline = now + timedelta(milliseconds=self.request_deadline_ms)
        margin = timedelta(seconds=self.dispatch_margin.seconds)
        return deadline - margin


def check_deadline(request: PendingRequest, now: datetime) -> None:
    """Raise DeadlineExceededError when a request can no longer be dispatched."""
    if request.is_expired(now):
        raise DeadlineExceededError(f"request {request.request_id!r} missed its dispatch deadline")
