"""Operator/emergency hard interrupt as a distinct control-plane path (task 14.10).

The arbiter's normal Q&A scheduling NEVER interrupts an active script
sentence (task 14.2). The operator/emergency hard interrupt is a SEPARATE
operation: it cancels active speech immediately and is not a pending-Q&A
candidate - it cannot be created through ``PendingQaStore.update`` and the
normal tick path never calls it. This module provides the minimal
``HardInterruptService`` boundary plus the operation-name guard; the arbiter
itself exposes ``SpeechArbiter.hard_stop`` (parallel task 14.1) as the
in-process entry, and the control plane wires ``POST /sessions/{id}/interrupt``
to the coordinator/orchestrator cancel path (canonical contract).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["HARD_INTERRUPT_OPERATION", "HardInterruptService", "is_hard_interrupt"]

HARD_INTERRUPT_OPERATION: str = "hard_interrupt"


@runtime_checkable
class CancellableSpeech(Protocol):
    """Structural view of a speech service with a cancel (barge-in) seam."""

    async def cancel(self, session_id: str) -> None: ...


def is_hard_interrupt(operation: str) -> bool:
    """Distinguish the control-plane hard interrupt from any Q&A scheduling op.

    Normal Q&A scheduling ops (schedule_qa, resolve_qa, boundary ticks) are
    never the hard interrupt; only the operator/control-plane operation
    string is.
    """
    return operation == HARD_INTERRUPT_OPERATION


class HardInterruptService:
    """Cancel active speech for a session via the speech service's cancel seam.

    Idempotent by delegation: the canonical ``StreamOrchestrator.cancel`` is a
    no-op when no turn is running for the session. This service never touches
    the pending-Q&A board and never creates a Q&A candidate - the hard
    interrupt is the operator's path, orthogonal to the arbiter's normal
    scheduling.
    """

    def __init__(self, speech: CancellableSpeech) -> None:
        self._speech = speech

    async def interrupt(self, session_id: str) -> None:
        await self._speech.cancel(session_id)


def assert_hard_interrupt_never_from_pending_qa(pending_qa_store: Any) -> None:
    """Guard: a hard interrupt can never be created through the pending board.

    ``PendingQaStore.update`` only accepts cluster envelopes; the hard
    interrupt has no cluster id and is not a Q&A candidate. This assertion
    documents the invariant so a future scheduler cannot route the operator
    stop through normal Q&A scheduling.
    """
    for cluster_id, _candidate in pending_qa_store._candidates.items():
        if cluster_id == HARD_INTERRUPT_OPERATION:
            raise RuntimeError("hard interrupt must never be a pending-Q&A candidate")
