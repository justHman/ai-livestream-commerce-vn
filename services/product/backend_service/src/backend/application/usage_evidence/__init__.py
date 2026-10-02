"""P0-FB-017: durable signed Runtime usage evidence (C-USAGE-EVIDENCE-001).

Evidence, not billing. Default off; fail closed. The Runtime never computes Live
Credits, prices, balances or billable durations (BR-PRICING-001/002).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from backend.application.execution_contract import (
    CommandOutcome,
    Evidence,
    ExecutionIdentity,
    ExecutionState,
)

from . import envelope
from .outbox import Staged, UsageOutbox
from .sender import COMMITTED_KEY, COMMITTED_MAX, UsageSender
from .settings import CogsBuffer, CogsSample, UsageEvidenceSettings

__all__ = [
    "UsageEvidence",
    "UsageEvidenceRejected",
    "UsageEvidenceUnavailable",
    "UsageEvidenceSettings",
    "UsageSender",
]

logger = logging.getLogger(__name__)


class UsageEvidenceUnavailable(Exception):
    """The durable row could not be staged; the fact must not be applied unreported."""


class UsageEvidenceRejected(Exception):
    """The fact can never be reported (undeliverable identity): refuse it, never apply it."""


class UsageEvidence:
    """Control-plane facade used by ``api/v1/execution.py`` (never by the speech path)."""

    def __init__(self, outbox: UsageOutbox, settings: UsageEvidenceSettings) -> None:
        self._outbox = outbox
        self.settings = settings
        self.cogs = CogsBuffer(settings.cogs_buffer)
        self.rejected_invalid_identity = 0

    async def _stage(self, updated: ExecutionState, drafts: list[envelope.Draft]) -> list[Staged]:
        if not drafts:
            return []
        try:
            return await self._outbox.stage(envelope.identity_of(updated), drafts)
        except envelope.InvalidIdentity as exc:
            # Can never be delivered. Fail closed: state must not advance without a row.
            self.rejected_invalid_identity += 1
            logger.error("usage evidence refused undeliverable identity: %s", exc)
            raise UsageEvidenceRejected(str(exc)) from exc
        except Exception as exc:
            logger.error("usage evidence stage failed error_type=%s", type(exc).__name__)
            raise UsageEvidenceUnavailable(type(exc).__name__) from exc

    async def stage_evidence(
        self, prior: ExecutionState, updated: ExecutionState, ev: Evidence
    ) -> list[Staged]:
        """Call only after ``apply_evidence`` succeeded and before the state is saved."""
        drafts = envelope.derive_from_evidence(prior, updated, ev, self.settings.gates)
        return await self._stage(updated, drafts)

    async def stage_command(
        self, prior: ExecutionState, updated: ExecutionState, outcome: CommandOutcome
    ) -> list[Staged]:
        """Call only for an applied outcome, before the state is saved."""
        drafts = envelope.derive_from_command(prior, updated, outcome, self.settings.gates)
        return await self._stage(updated, drafts)

    async def commit(self, staged: Sequence[Staged]) -> None:
        """After the save: ``staged`` -> ``ready``. Best effort; the sweeper converges."""
        if not staged:
            return
        try:
            await self._outbox.mark_ready([s.event_id for s in staged])
        except Exception as exc:
            logger.warning(
                "usage evidence ready flip deferred to sweeper error_type=%s", type(exc).__name__
            )

    @staticmethod
    def stamp(meta: dict[str, Any], staged: Sequence[Staged]) -> None:
        """Record the facts this save commits, in the SAME atomic save as the state.

        The sweeper marks a staged row ready only if its event_id is listed here.
        """
        if not staged:
            return
        prior = list(meta.get(COMMITTED_KEY) or [])
        ids = [s.event_id for s in staged]
        meta[COMMITTED_KEY] = ([i for i in prior if i not in ids] + ids)[-COMMITTED_MAX:]

    async def abort(self, staged: Sequence[Staged]) -> None:
        """The save DEFINITELY did not land: remove the staged rows. Best effort.

        Never call this for an ambiguous outcome; leave the rows for the sweeper.
        """
        if not staged:
            return
        try:
            await self._outbox.release([s.event_id for s in staged], delete=True)
        except Exception as exc:
            logger.warning(
                "usage evidence abort deferred to sweeper error_type=%s", type(exc).__name__
            )

    def record_cogs(
        self,
        identity: ExecutionIdentity,
        *,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        sample_id: str | None = None,
    ) -> None:
        """Speech-path safe: append to a bounded in-memory buffer. No await, no I/O."""
        self.cogs.append(
            CogsSample(
                identity,
                sample_id or uuid.uuid4().hex,
                datetime.now(timezone.utc),
                model_id,
                input_tokens,
                output_tokens,
            )
        )

    async def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "cogs_buffered": len(self.cogs),
            "cogs_dropped": self.cogs.dropped,
        }
        try:
            out.update(await self._outbox.backlog())
        except Exception as exc:
            out["backlog_error"] = type(exc).__name__
        return out
