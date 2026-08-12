"""Scheduler subpackage: admission accounting, pending state, fair selection.

Owned by Change T cluster 6. The continuous dispatch runtime (cluster 7)
consumes these primitives; nothing here talks to providers, HTTP, or the
event loop beyond the asyncio.Future carried by PendingRequest.
"""

from tts.scheduler.admission import AdmissionController, DispatchMargin, check_deadline
from tts.scheduler.fairness import (
    FairnessConfig,
    FairnessSelector,
    PendingPopulation,
)
from tts.scheduler.models import (
    InFlightBatch,
    PendingRequest,
    PendingState,
    same_request,
)

__all__ = [
    "AdmissionController",
    "DispatchMargin",
    "FairnessConfig",
    "FairnessSelector",
    "InFlightBatch",
    "PendingPopulation",
    "PendingRequest",
    "PendingState",
    "check_deadline",
    "same_request",
]
