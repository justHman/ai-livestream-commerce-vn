"""Scheduler-side pending/in-flight request state (Change T task 8.1).

``SynthesisRequest`` is the immutable identity; ``PendingRequest`` adds the
scheduler's mutable lifecycle around it: completion future, cancellation flag,
state, admission bookkeeping, effective dispatch deadline, and the provider
batch key. ``InFlightBatch`` is the immutable-at-dispatch unit the runtime
hands to a provider.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Hashable, Optional

from tts.providers.base import TTSProvider
from tts.providers.models import AudioResult, SynthesisRequest


class PendingState(str, Enum):
    """Lifecycle states of a pending request."""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class PendingRequest:
    """Mutable scheduler lifecycle for one immutable ``SynthesisRequest``."""

    synthesis_request: SynthesisRequest
    completion: asyncio.Future[AudioResult] = field(default_factory=asyncio.Future)
    cancelled: bool = False
    state: PendingState = PendingState.PENDING
    admitted_at: Optional[datetime] = None
    dispatch_deadline: Optional[datetime] = None
    provider_batch_key: Optional[Hashable] = None

    @property
    def request_id(self) -> str:
        return self.synthesis_request.request_id

    @property
    def session_id(self) -> str:
        return self.synthesis_request.session_id

    def is_expired(self, now: datetime) -> bool:
        """True when the effective dispatch deadline has passed.

        ``dispatch_deadline`` is the admission-time bound (deadline minus a
        dispatch safety margin); it is earlier than the request deadline so
        the runtime can dispatch a request early before violating it.
        """
        effective = self.dispatch_deadline or self.synthesis_request.deadline_at
        if effective is None:
            return False
        return now >= effective


@dataclass(frozen=True)
class InFlightBatch:
    """One static provider batch, immutable once dispatched."""

    batch_key: Hashable
    members: tuple[PendingRequest, ...]
    dispatched_at: datetime
    provider: TTSProvider


def same_request(left: PendingRequest, right: PendingRequest) -> bool:
    """Identity equality: a request is the same request iff its ID matches.

    Dataclass ``__eq__`` compares the mutable wrapper too, which is wrong for
    duplicate-ID detection: the whole request (not just the wrapper) must
    match, and request ID is globally unique.
    """
    return left.request_id == right.request_id
